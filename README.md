# Garmin Shoe Sync

Copy the shoes you wore on past Strava runs onto the matching activities in Garmin Connect.

The script does **not** create shoes. Add every pair in Garmin Connect first, then use this to attach those existing shoes to historical runs.

This talks to Garmin through the unofficial [garminconnect](https://github.com/cyberjunky/python-garminconnect) library. Use it on your own account.

## What you need

- Python 3.10 or newer
- A Garmin Connect account, with your shoes already added
- A Strava data export (`activities.csv`)
- Your Garmin email and password (stored locally in `.env`, never committed)

## Setup

```bash
git clone https://github.com/YOUR_USERNAME/garmin-shoe-sync.git
cd garmin-shoe-sync

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
```

Open `.env` and replace the placeholders with your Garmin login:

```dotenv
GARMIN_EMAIL=you@example.com
GARMIN_PASSWORD=your-garmin-password
```

Do not commit `.env`. Git is already set up to ignore it.

### Get your Strava export

1. In Strava, go to **Settings → My Account → Download or Delete Your Account**.
2. Request the archive, then unzip it.
3. Copy `activities.csv` into this project folder.

The file must include `Activity Type`, `Activity Date`, and a shoe column named `Gear` or `Activity Gear`. Detailed `Distance.*` columns (meters) are used when you have more than one run on the same day.

`activities.csv` is personal data and is gitignored.

## Run

Replace `America/New_York` with [your IANA timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones). Strava timestamps are UTC; this timezone is how the script lines them up with Garmin's local dates.

### 1. Create the shoe mapping

```bash
python sync_shoes.py --dry-run --strava-timezone America/New_York
```

The first time you run this, the script:

1. Logs into Garmin (it may ask for an MFA code in the terminal)
2. Reads unique shoe names from your Strava CSV
3. Fetches gear from Garmin Connect
4. Writes suggested matches to `gear_mapping.json`
5. Exits so you can review the file

Session tokens are saved under `~/.garminconnect` and reused later. Garmin sometimes returns HTTP 429 on a fresh login; wait and retry, or reuse the cached session.

### 2. Review `gear_mapping.json`

Open the file and check every `garmin_name` and `gear_uuid`. A `null` UUID skips that Strava shoe. Garmin names fall back to `customMakeModel` when `displayName` is empty.

```json
{
  "version": 1,
  "mappings": {
    "Strava shoe name": {
      "garmin_name": "Garmin shoe name",
      "gear_uuid": "garmin-gear-uuid"
    },
    "Skip this pair": {
      "garmin_name": null,
      "gear_uuid": null
    }
  }
}
```

Each `gear_uuid` must match a shoe that already exists in Garmin Connect. See `gear_mapping.example.json` for a complete example. Your real mapping file is gitignored because it is account-specific.

### 3. Dry-run the links

```bash
python sync_shoes.py --dry-run --strava-timezone America/New_York
```

This prints each activity and shoe it would link, plus any unmatched rows. Nothing is written to Garmin.

### 4. Apply the links

```bash
python sync_shoes.py --strava-timezone America/New_York
```

Live updates wait one second between Garmin writes. A failed link is logged and the rest of the batch continues.

## Optional: test one activity first

```bash
python sync_shoes.py --dry-run --strava-timezone America/New_York --activity-id 12345678901
python sync_shoes.py --strava-timezone America/New_York --activity-id 12345678901
```

Repeat `--activity-id` to process a small batch. If a requested ID cannot be matched safely, the script exits without linking anything.

## Optional: remove a duplicate shoe

If an activity has the wrong extra shoe attached, you can remove it while keeping the mapped pair:

```bash
python sync_shoes.py --dry-run --strava-timezone America/New_York \
  --remove-duplicate-gear "ASICS Novablast 5"
python sync_shoes.py --strava-timezone America/New_York \
  --remove-duplicate-gear "ASICS Novablast 5"
```

Combine this with `--activity-id` to test one activity first.

## How matching works

Only Strava rows with `Activity Type` of `Run` and a populated shoe name are considered. Garmin activities are fetched as `running` for the Strava date range, plus one day of padding on each side.

Activities are matched one-to-one in this order:

1. UTC start time within 120 seconds (`--tolerance` to change this).
2. The only remaining Strava and Garmin run on the same local date.
3. For dates with multiple remaining runs, the closest distance within 10% or 200 meters, whichever is larger. Ties within 50 meters or 2% of the Strava distance are treated as ambiguous and skipped.

Without `--strava-timezone`, this computer's timezone is used.

Strava exports label the shoe field differently across versions. The script auto-detects `Gear` or `Activity Gear`; override with `--gear-column "Activity Gear"` if needed.

## Options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--csv` | `activities.csv` | Path to the Strava export |
| `--mapping` | `gear_mapping.json` | Reviewed Strava-to-Garmin shoe map |
| `--token-store` | `~/.garminconnect` | Cached Garmin session directory |
| `--strava-timezone` | this computer's timezone | IANA zone for local-date matching |
| `--gear-column` | auto-detect | CSV column with Strava shoe names |
| `--tolerance` | `120` | Maximum start-time delta in seconds |
| `--dry-run` | off | Print planned links without writing |
| `--activity-id` | all matches | Limit to one Garmin activity; repeatable |
| `--remove-duplicate-gear` | none | Remove this gear unless it is the mapped shoe; repeatable |

## Privacy

These files stay on your computer and are listed in `.gitignore` so they are not uploaded:

- `.env` — Garmin email and password
- `activities.csv` — your Strava export
- `gear_mapping.json` — your shoe names and Garmin gear IDs
- `~/.garminconnect` — cached login tokens

If you already committed any of those, remove them from git history and change your Garmin password before making the repo public.

## Troubleshooting

**Missing credentials.** Copy `.env.example` to `.env` and set `GARMIN_EMAIL` and `GARMIN_PASSWORD`.

**MFA prompt.** Type the code from Garmin when asked. Later runs reuse `~/.garminconnect`.

**HTTP 429.** Garmin rate-limited a fresh login. Wait a few minutes and retry.

**No shoes found.** Add the pairs in Garmin Connect first, then delete `gear_mapping.json` and generate it again.

**Unmatched runs.** Check `--strava-timezone`, confirm both activities exist, and look at the dry-run log. Ambiguous same-day doubles are skipped on purpose.

**CSV column errors.** Point `--csv` at the unzipped Strava `activities.csv`, or set `--gear-column` if your export uses a different shoe column name.

## License

MIT. See [LICENSE](LICENSE).
