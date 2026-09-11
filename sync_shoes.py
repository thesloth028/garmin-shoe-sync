#!/usr/bin/env python3
"""Copy historical running-shoe assignments from a Strava export to Garmin."""

from __future__ import annotations

import argparse
import inspect
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
from dotenv import load_dotenv
from garminconnect import Garmin


LOGGER = logging.getLogger("garmin-shoe-sync")
MAPPING_VERSION = 1
LINK_URL = (
    "https://connect.garmin.com/modern/proxy/gear-service/gear/link/"
    "{gear_uuid}/activity/{activity_id}"
)


@dataclass(frozen=True)
class StravaRun:
    row_number: int
    name: str
    gear_name: str
    start_utc: datetime
    local_date: date
    distance_meters: float | None


@dataclass(frozen=True)
class GarminRun:
    activity_id: str
    name: str
    start_utc: datetime
    local_date: date
    distance_meters: float | None


@dataclass(frozen=True)
class Match:
    strava: StravaRun
    garmin: GarminRun
    delta_seconds: float
    method: str
    distance_delta_meters: float | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Sync historical Strava running shoes to Garmin Connect."
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path("activities.csv"),
        help="Strava activities CSV (default: activities.csv)",
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        default=Path("gear_mapping.json"),
        help="Reviewed gear mapping file (default: gear_mapping.json)",
    )
    parser.add_argument(
        "--token-store",
        type=Path,
        default=Path(os.getenv("GARMINTOKENS", "~/.garminconnect")).expanduser(),
        help="Garmin/Garth session token directory (default: ~/.garminconnect)",
    )
    parser.add_argument(
        "--strava-timezone",
        help=(
            "IANA timezone used to derive local dates from Strava's UTC times, "
            "such as America/New_York (default: this computer's local timezone)"
        ),
    )
    parser.add_argument(
        "--gear-column",
        help="CSV column containing shoe names (default: auto-detect)",
    )
    parser.add_argument(
        "--tolerance",
        type=int,
        default=120,
        help="Maximum start-time difference in seconds (default: 120)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned changes without linking any Garmin gear",
    )
    parser.add_argument(
        "--activity-id",
        action="append",
        dest="activity_ids",
        help=(
            "Only process this Garmin activity ID; repeat for multiple activities"
        ),
    )
    parser.add_argument(
        "--remove-duplicate-gear",
        action="append",
        metavar="GARMIN_GEAR_NAME",
        help=(
            "Remove this gear from matched activities whose reviewed mapping "
            "specifies a different shoe; repeat for multiple gear names"
        ),
    )
    args = parser.parse_args()
    if args.tolerance < 0:
        parser.error("--tolerance must be zero or greater")
    return args


def authenticate(token_store: Path) -> Garmin:
    email = os.getenv("GARMIN_EMAIL") or os.getenv("EMAIL")
    password = os.getenv("GARMIN_PASSWORD") or os.getenv("PASSWORD")
    if not email or not password:
        raise RuntimeError(
            "Missing Garmin credentials. Set GARMIN_EMAIL and GARMIN_PASSWORD in .env."
        )

    token_store.mkdir(parents=True, exist_ok=True)
    client = Garmin(
        email=email,
        password=password,
        prompt_mfa=lambda: input("Garmin MFA code: ").strip(),
    )
    LOGGER.info("Authenticating with Garmin Connect...")
    client.login(str(token_store))
    LOGGER.info("Authenticated; session tokens are cached in %s", token_store)
    return client


def get_garmin_gear(client: Garmin) -> list[dict[str, Any]]:
    """Support both old get_gear() and new get_gear(profile_number) APIs."""
    parameters = inspect.signature(client.get_gear).parameters
    if not parameters:
        raw_gear = client.get_gear()
    else:
        device = client.get_device_last_used()
        profile_number = device.get("userProfileNumber") if device else None
        if not profile_number:
            raise RuntimeError("Garmin did not return a user profile number.")
        raw_gear = client.get_gear(profile_number)

    if isinstance(raw_gear, list):
        gear_items = raw_gear
    elif isinstance(raw_gear, dict):
        gear_items = next(
            (
                value
                for key, value in raw_gear.items()
                if key.lower() in {"gear", "gearlist", "items"}
                and isinstance(value, list)
            ),
            [],
        )
    else:
        gear_items = []

    usable = []
    for item in gear_items:
        if not isinstance(item, dict) or not item.get("uuid"):
            continue
        display_name = item.get("displayName")
        if not display_name:
            display_name = item.get("customMakeModel")
        if display_name:
            usable.append({**item, "displayName": str(display_name).strip()})

    if not usable:
        raise RuntimeError("No Garmin gear with both a display name and UUID was found.")
    LOGGER.info("Found %d pieces of Garmin gear.", len(usable))
    return usable


def load_csv(csv_path: Path) -> pd.DataFrame:
    if not csv_path.is_file():
        raise FileNotFoundError(f"Strava CSV not found: {csv_path}")
    frame = pd.read_csv(csv_path, low_memory=False)
    required = {"Activity Type", "Activity Date"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")
    return frame


def populated_text_count(series: pd.Series) -> int:
    values = series.dropna().astype(str).str.strip()
    values = values[values.ne("")]
    return int((pd.to_numeric(values, errors="coerce").isna()).sum())


def choose_gear_column(frame: pd.DataFrame, requested: str | None) -> str:
    if requested:
        if requested not in frame.columns:
            raise ValueError(f"CSV has no gear column named {requested!r}.")
        return requested

    candidates = [
        column for column in ("Gear", "Activity Gear") if column in frame.columns
    ]
    if not candidates:
        raise ValueError("CSV has neither a 'Gear' nor an 'Activity Gear' column.")
    selected = max(candidates, key=lambda column: populated_text_count(frame[column]))
    LOGGER.info("Using CSV column %r for Strava shoe names.", selected)
    return selected


def choose_distance_column(frame: pd.DataFrame) -> str | None:
    detailed_columns = [
        column for column in frame.columns if column.startswith("Distance.")
    ]
    if not detailed_columns:
        LOGGER.warning(
            "No detailed distance column found; multi-run dates will be skipped."
        )
        return None
    selected = detailed_columns[0]
    LOGGER.info("Using CSV column %r for distance matching.", selected)
    return selected


def unique_strava_gear(frame: pd.DataFrame, gear_column: str) -> list[str]:
    runs = frame[frame["Activity Type"].astype(str).str.casefold().eq("run")]
    values = runs[gear_column].dropna().astype(str).str.strip()
    values = values[values.ne("")]
    return sorted(values.unique().tolist(), key=str.casefold)


def create_mapping(
    mapping_path: Path,
    strava_names: list[str],
    garmin_gear: list[dict[str, Any]],
) -> None:
    from difflib import get_close_matches

    by_name = {str(item["displayName"]): str(item["uuid"]) for item in garmin_gear}
    garmin_names = list(by_name)
    mappings: dict[str, dict[str, str | None]] = {}
    for strava_name in strava_names:
        suggestions = get_close_matches(
            strava_name, garmin_names, n=1, cutoff=0.35
        )
        suggestion = suggestions[0] if suggestions else None
        mappings[strava_name] = {
            "garmin_name": suggestion,
            "gear_uuid": by_name.get(suggestion) if suggestion else None,
        }

    document = {
        "version": MAPPING_VERSION,
        "_instructions": (
            "Review every suggestion. Correct garmin_name and gear_uuid as needed; "
            "set gear_uuid to null to skip a shoe. Then run the script again."
        ),
        "mappings": mappings,
    }
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    mapping_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    LOGGER.info("Created %s with %d suggested mappings.", mapping_path, len(mappings))
    LOGGER.info("Review that file, then run this command again.")


def load_mapping(
    mapping_path: Path, garmin_gear: list[dict[str, Any]]
) -> dict[str, tuple[str, str]]:
    try:
        document = json.loads(mapping_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {mapping_path}: {exc}") from exc

    raw_mappings = document.get("mappings") if isinstance(document, dict) else None
    if not isinstance(raw_mappings, dict):
        raise ValueError(f"{mapping_path} must contain a JSON object named 'mappings'.")

    garmin_by_uuid = {
        str(item["uuid"]): str(item["displayName"]) for item in garmin_gear
    }
    mappings: dict[str, tuple[str, str]] = {}
    for strava_name, value in raw_mappings.items():
        if not isinstance(value, dict):
            raise ValueError(f"Mapping for {strava_name!r} must be an object.")
        uuid = value.get("gear_uuid")
        if uuid in (None, ""):
            LOGGER.warning("Skipping unmapped Strava gear %r.", strava_name)
            continue
        uuid = str(uuid)
        if uuid not in garmin_by_uuid:
            raise ValueError(
                f"Mapping for {strava_name!r} references unknown Garmin UUID {uuid!r}."
            )
        mappings[str(strava_name)] = (garmin_by_uuid[uuid], uuid)
    return mappings


def strava_time_to_utc(value: Any) -> datetime:
    timestamp = pd.to_datetime(value, errors="raise")
    if timestamp.tzinfo is not None:
        return timestamp.to_pydatetime().astimezone(timezone.utc)
    return timestamp.to_pydatetime().replace(tzinfo=timezone.utc)


def utc_to_local_date(value: datetime, timezone_name: str | None) -> date:
    if timezone_name:
        try:
            local_zone = ZoneInfo(timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone: {timezone_name}") from exc
        return value.astimezone(local_zone).date()
    return value.astimezone().date()


def extract_strava_runs(
    frame: pd.DataFrame,
    gear_column: str,
    distance_column: str | None,
    mappings: dict[str, tuple[str, str]],
    timezone_name: str | None,
) -> list[StravaRun]:
    runs: list[StravaRun] = []
    for index, row in frame.iterrows():
        if str(row.get("Activity Type", "")).casefold() != "run":
            continue
        gear_value = row.get(gear_column)
        if pd.isna(gear_value) or not str(gear_value).strip():
            continue
        gear_name = str(gear_value).strip()
        if gear_name not in mappings:
            LOGGER.warning(
                "Skipping CSV row %d: no reviewed mapping for %r.",
                index + 2,
                gear_name,
            )
            continue
        try:
            start_utc = strava_time_to_utc(row["Activity Date"])
            local_date = utc_to_local_date(start_utc, timezone_name)
        except (TypeError, ValueError) as exc:
            LOGGER.warning("Skipping CSV row %d: invalid Activity Date (%s).", index + 2, exc)
            continue
        distance_meters = None
        if distance_column:
            distance_value = pd.to_numeric(row.get(distance_column), errors="coerce")
            if pd.notna(distance_value) and float(distance_value) > 0:
                distance_meters = float(distance_value)
        runs.append(
            StravaRun(
                row_number=index + 2,
                name=str(row.get("Activity Name") or "Unnamed run"),
                gear_name=gear_name,
                start_utc=start_utc,
                local_date=local_date,
                distance_meters=distance_meters,
            )
        )
    return runs


def parse_garmin_start(activity: dict[str, Any]) -> datetime | None:
    value = activity.get("startTimeGMT")
    if not value:
        return None
    try:
        parsed = pd.to_datetime(value, utc=True, errors="raise")
    except (TypeError, ValueError):
        return None
    return parsed.to_pydatetime()


def parse_garmin_local_date(
    activity: dict[str, Any], start_utc: datetime
) -> date:
    value = activity.get("startTimeLocal")
    if value:
        try:
            return pd.to_datetime(value, errors="raise").date()
        except (TypeError, ValueError):
            pass
    return start_utc.astimezone().date()


def fetch_garmin_runs(client: Garmin, strava_runs: list[StravaRun]) -> list[GarminRun]:
    first = min(run.start_utc for run in strava_runs) - timedelta(days=1)
    last = max(run.start_utc for run in strava_runs) + timedelta(days=1)
    LOGGER.info(
        "Fetching Garmin running activities from %s through %s...",
        first.date(),
        last.date(),
    )
    activities = client.get_activities_by_date(
        first.date().isoformat(),
        last.date().isoformat(),
        activitytype="running",
    )
    runs: list[GarminRun] = []
    for activity in activities:
        activity_id = activity.get("activityId")
        start_utc = parse_garmin_start(activity)
        if activity_id is None or start_utc is None:
            continue
        distance_value = pd.to_numeric(activity.get("distance"), errors="coerce")
        distance_meters = (
            float(distance_value)
            if pd.notna(distance_value) and float(distance_value) > 0
            else None
        )
        runs.append(
            GarminRun(
                activity_id=str(activity_id),
                name=str(activity.get("activityName") or "Unnamed run"),
                start_utc=start_utc,
                local_date=parse_garmin_local_date(activity, start_utc),
                distance_meters=distance_meters,
            )
        )
    LOGGER.info("Fetched %d Garmin running activities.", len(runs))
    return runs


def match_runs(
    strava_runs: list[StravaRun],
    garmin_runs: list[GarminRun],
    tolerance_seconds: int,
) -> tuple[list[Match], list[StravaRun]]:
    time_candidates: list[tuple[float, int, int]] = []
    for strava_index, strava_run in enumerate(strava_runs):
        for garmin_index, garmin_run in enumerate(garmin_runs):
            delta = abs(
                (strava_run.start_utc - garmin_run.start_utc).total_seconds()
            )
            if delta <= tolerance_seconds:
                time_candidates.append((delta, strava_index, garmin_index))

    matched_strava: set[int] = set()
    matched_garmin: set[int] = set()
    matches: list[Match] = []

    # Prefer precise start-time matches wherever they exist.
    for delta, strava_index, garmin_index in sorted(time_candidates):
        if strava_index in matched_strava or garmin_index in matched_garmin:
            continue
        matched_strava.add(strava_index)
        matched_garmin.add(garmin_index)
        matches.append(
            Match(
                strava=strava_runs[strava_index],
                garmin=garmin_runs[garmin_index],
                delta_seconds=delta,
                method="start time",
            )
        )

    strava_by_date: dict[date, list[int]] = {}
    garmin_by_date: dict[date, list[int]] = {}
    for index, run in enumerate(strava_runs):
        if index not in matched_strava:
            strava_by_date.setdefault(run.local_date, []).append(index)
    for index, run in enumerate(garmin_runs):
        if index not in matched_garmin:
            garmin_by_date.setdefault(run.local_date, []).append(index)

    # A date containing exactly one remaining run on each service is unambiguous.
    for local_date, strava_indices in strava_by_date.items():
        garmin_indices = garmin_by_date.get(local_date, [])
        if len(strava_indices) != 1 or len(garmin_indices) != 1:
            continue
        strava_index = strava_indices[0]
        garmin_index = garmin_indices[0]
        strava_run = strava_runs[strava_index]
        garmin_run = garmin_runs[garmin_index]
        distance_delta = (
            abs(strava_run.distance_meters - garmin_run.distance_meters)
            if strava_run.distance_meters is not None
            and garmin_run.distance_meters is not None
            else None
        )
        matched_strava.add(strava_index)
        matched_garmin.add(garmin_index)
        matches.append(
            Match(
                strava=strava_run,
                garmin=garmin_run,
                delta_seconds=abs(
                    (strava_run.start_utc - garmin_run.start_utc).total_seconds()
                ),
                method="unique local date",
                distance_delta_meters=distance_delta,
            )
        )

    # On dates with multiple runs, pair by nearest distance. Reject implausible
    # differences and cases where two Garmin candidates are effectively tied.
    distance_candidates: list[tuple[float, float, int, int]] = []
    for local_date, strava_indices in strava_by_date.items():
        remaining_strava = [
            index for index in strava_indices if index not in matched_strava
        ]
        remaining_garmin = [
            index
            for index in garmin_by_date.get(local_date, [])
            if index not in matched_garmin
        ]
        if not remaining_strava or not remaining_garmin:
            continue

        for strava_index in remaining_strava:
            strava_run = strava_runs[strava_index]
            if strava_run.distance_meters is None:
                continue
            candidate_differences: list[tuple[float, int]] = []
            for garmin_index in remaining_garmin:
                garmin_run = garmin_runs[garmin_index]
                if garmin_run.distance_meters is None:
                    continue
                difference = abs(
                    strava_run.distance_meters - garmin_run.distance_meters
                )
                allowed_difference = max(200.0, strava_run.distance_meters * 0.10)
                if difference <= allowed_difference:
                    candidate_differences.append((difference, garmin_index))

            candidate_differences.sort()
            ambiguity_margin = max(50.0, strava_run.distance_meters * 0.02)
            if (
                len(candidate_differences) > 1
                and candidate_differences[1][0] - candidate_differences[0][0]
                <= ambiguity_margin
            ):
                LOGGER.warning(
                    "Ambiguous distance match on %s for %s (CSV row %d); skipping.",
                    local_date,
                    strava_run.name,
                    strava_run.row_number,
                )
                continue

            for difference, garmin_index in candidate_differences:
                garmin_run = garmin_runs[garmin_index]
                time_delta = abs(
                    (strava_run.start_utc - garmin_run.start_utc).total_seconds()
                )
                normalized_difference = difference / strava_run.distance_meters
                distance_candidates.append(
                    (
                        normalized_difference,
                        time_delta,
                        strava_index,
                        garmin_index,
                    )
                )

    for _, time_delta, strava_index, garmin_index in sorted(distance_candidates):
        if strava_index in matched_strava or garmin_index in matched_garmin:
            continue
        strava_run = strava_runs[strava_index]
        garmin_run = garmin_runs[garmin_index]
        distance_delta = abs(
            strava_run.distance_meters - garmin_run.distance_meters
        )
        matched_strava.add(strava_index)
        matched_garmin.add(garmin_index)
        matches.append(
            Match(
                strava=strava_run,
                garmin=garmin_run,
                delta_seconds=time_delta,
                method="same-date distance",
                distance_delta_meters=distance_delta,
            )
        )

    matches.sort(key=lambda match: match.strava.start_utc)
    unmatched = [
        run for index, run in enumerate(strava_runs) if index not in matched_strava
    ]
    return matches, unmatched


def link_gear(client: Garmin, gear_uuid: str, activity_id: str) -> None:
    url = LINK_URL.format(gear_uuid=gear_uuid, activity_id=activity_id)
    garth = getattr(client, "garth", None)
    if garth is not None:
        # API used by garminconnect releases that expose Garth directly.
        garth.put("connect", url, api=True)
    elif hasattr(client, "add_gear_to_activity"):
        # Current garminconnect exposes the same endpoint as a public method.
        client.add_gear_to_activity(gear_uuid, activity_id)
    else:
        relative_url = f"/gear-service/gear/link/{gear_uuid}/activity/{activity_id}"
        client.client.put("connectapi", relative_url)


def remove_duplicate_gear(
    client: Garmin,
    matches: list[Match],
    mappings: dict[str, tuple[str, str]],
    garmin_gear: list[dict[str, Any]],
    target_names: list[str],
    dry_run: bool,
) -> tuple[int, int]:
    gear_by_name = {
        str(item["displayName"]).casefold(): (
            str(item["displayName"]),
            str(item["uuid"]),
        )
        for item in garmin_gear
    }
    targets: list[tuple[str, str]] = []
    for requested_name in target_names:
        target = gear_by_name.get(requested_name.casefold())
        if target is None:
            raise ValueError(f"Garmin gear not found: {requested_name!r}")
        targets.append(target)

    removals = 0
    failures = 0
    for match in matches:
        desired_name, desired_uuid = mappings[match.strava.gear_name]
        removable_targets = [
            target for target in targets if target[1] != desired_uuid
        ]
        if not removable_targets:
            continue

        activity_gear = client.get_activity_gear(match.garmin.activity_id)
        if not isinstance(activity_gear, list):
            LOGGER.warning(
                "Skipping activity %s: Garmin returned unexpected gear data.",
                match.garmin.activity_id,
            )
            continue
        attached_uuids = {
            str(item.get("uuid"))
            for item in activity_gear
            if isinstance(item, dict) and item.get("uuid")
        }

        for target_name, target_uuid in removable_targets:
            if target_uuid not in attached_uuids:
                continue
            prefix = "WOULD REMOVE" if dry_run else "REMOVE"
            LOGGER.info(
                "%s %s from %s on %s, Garmin activity %s; keeping %s",
                prefix,
                target_name,
                match.strava.name,
                match.strava.local_date,
                match.garmin.activity_id,
                desired_name,
            )
            if dry_run:
                removals += 1
                continue
            try:
                client.remove_gear_from_activity(
                    target_uuid, match.garmin.activity_id
                )
            except Exception as exc:
                failures += 1
                LOGGER.error(
                    "Failed to remove %s from activity %s: %s",
                    target_name,
                    match.garmin.activity_id,
                    exc,
                )
            else:
                removals += 1
                LOGGER.info("Removed successfully.")
            time.sleep(1.0)
    return removals, failures


def execute(
    client: Garmin,
    matches: list[Match],
    mappings: dict[str, tuple[str, str]],
    dry_run: bool,
) -> tuple[int, int]:
    successes = 0
    failures = 0
    total = len(matches)
    for position, match in enumerate(matches, start=1):
        garmin_gear_name, gear_uuid = mappings[match.strava.gear_name]
        prefix = "WOULD LINK" if dry_run else "LINK"
        distance_detail = (
            f", distance delta {match.distance_delta_meters:.0f}m"
            if match.distance_delta_meters is not None
            else ""
        )
        LOGGER.info(
            "[%d/%d] %s %s -> %s (%s), Garmin activity %s; "
            "matched by %s (start delta %.0fs%s)",
            position,
            total,
            prefix,
            match.strava.name,
            garmin_gear_name,
            gear_uuid,
            match.garmin.activity_id,
            match.method,
            match.delta_seconds,
            distance_detail,
        )
        if dry_run:
            successes += 1
            continue
        try:
            link_gear(client, gear_uuid, match.garmin.activity_id)
        except Exception as exc:  # Continue so one Garmin error does not end the batch.
            failures += 1
            LOGGER.error("Failed to link activity %s: %s", match.garmin.activity_id, exc)
        else:
            successes += 1
            LOGGER.info("Linked successfully.")
        if position < total:
            time.sleep(1.0)
    return successes, failures


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    load_dotenv()

    try:
        frame = load_csv(args.csv)
        gear_column = choose_gear_column(frame, args.gear_column)
        distance_column = choose_distance_column(frame)
        client = authenticate(args.token_store)
        garmin_gear = get_garmin_gear(client)

        if not args.mapping.exists():
            create_mapping(
                args.mapping,
                unique_strava_gear(frame, gear_column),
                garmin_gear,
            )
            return 0

        mappings = load_mapping(args.mapping, garmin_gear)
        strava_runs = extract_strava_runs(
            frame,
            gear_column,
            distance_column,
            mappings,
            args.strava_timezone,
        )
        if not strava_runs:
            LOGGER.info("No mapped Strava runs to process.")
            return 0

        garmin_runs = fetch_garmin_runs(client, strava_runs)
        matches, unmatched = match_runs(strava_runs, garmin_runs, args.tolerance)
        if args.activity_ids:
            requested_ids = set(args.activity_ids)
            matches = [
                match
                for match in matches
                if match.garmin.activity_id in requested_ids
            ]
            found_ids = {match.garmin.activity_id for match in matches}
            missing_ids = requested_ids - found_ids
            if missing_ids:
                raise ValueError(
                    "Requested Garmin activity IDs were not safely matched: "
                    + ", ".join(sorted(missing_ids))
                )
            unmatched = []
            LOGGER.info(
                "Restricted execution to %d requested Garmin activity ID(s).",
                len(matches),
            )
        for run in unmatched:
            LOGGER.warning(
                "No unambiguous Garmin match: %s on %s at %s (CSV row %d).",
                run.name,
                run.local_date,
                run.start_utc.isoformat(),
                run.row_number,
            )

        if not matches:
            LOGGER.info("No matching Garmin activities found.")
            return 0
        if args.remove_duplicate_gear:
            successes, failures = remove_duplicate_gear(
                client,
                matches,
                mappings,
                garmin_gear,
                args.remove_duplicate_gear,
                args.dry_run,
            )
            action = "planned removals" if args.dry_run else "removed"
            LOGGER.info(
                "Cleanup finished: %d %s, %d failed.",
                successes,
                action,
                failures,
            )
            return 1 if failures else 0

        successes, failures = execute(client, matches, mappings, args.dry_run)
        mode = "planned" if args.dry_run else "linked"
        LOGGER.info(
            "Finished: %d %s, %d unmatched, %d failed.",
            successes,
            mode,
            len(unmatched),
            failures,
        )
        return 1 if failures else 0
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        LOGGER.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        LOGGER.error("Cancelled.")
        return 130
    except Exception as exc:
        LOGGER.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
