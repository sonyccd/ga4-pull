# ga4-pull

Export historical Google Analytics 4 report data to CSV before the property goes away.

You describe the reports you want as "buckets" (a set of dimensions and metrics) in a YAML file. The tool runs each bucket against each property through the GA4 Data API, always broken down by day, and writes one CSV per property per bucket. It handles pagination, backs off on quota errors, skips work that is already done, and reports what failed at the end.

## Requirements

- Python 3.10 or newer
- A Google Cloud service account with the **Google Analytics Data API** and **Google Analytics Admin API** enabled on its project
- That service account added as a **Viewer** on each GA4 property (or on the account) you want to export

## Install

```sh
git clone https://github.com/sonyccd/ga4-pull.git
cd ga4-pull
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Authentication

The tool uses Application Default Credentials. Point the standard environment variable at the service account's JSON key:

```sh
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

Nothing else is configured for auth. If the variable is unset or the file is unreadable the tool exits with a one-line message.

## Usage

### 1. Find your property IDs

```sh
python ga_export.py list-properties
```

This prints every property the service account can see:

```
ACCOUNT    PROPERTY_ID  PROPERTY_NAME
Acme Inc   123456789    acme.com
Acme Inc   987654321    Acme mobile app
```

If a property you expect is missing, the service account has not been granted access to it in GA4 Admin.

### 2. Run the export

```sh
python ga_export.py extract \
  --properties 123456789,987654321 \
  --start-date 2023-01-01 \
  --end-date 2025-12-31
```

| Flag | Required | Default | Meaning |
|---|---|---|---|
| `--properties` | yes | | Comma-separated GA4 property IDs. `properties/123` and `123` are both accepted; duplicates are ignored. |
| `--start-date` | yes | | First day to export, `YYYY-MM-DD`. |
| `--end-date` | yes | | Last day to export, inclusive, `YYYY-MM-DD`. Must not be in the future. |
| `--config` | no | `buckets.yaml` | Bucket definitions file. |
| `--out` | no | `./export` | Output directory. |

Progress is logged to stdout as it goes:

```
2026-09-30 10:41:02,113 INFO [123456789/acquisition] starting (2023-01-01 to 2025-12-31)
2026-09-30 10:41:04,507 INFO [123456789/acquisition] fetched 250000/341873 rows
2026-09-30 10:41:06,891 INFO [123456789/acquisition] fetched 341873/341873 rows
2026-09-30 10:41:08,231 INFO [123456789/acquisition] done: 341873 rows -> ./export/123456789/acquisition.csv
```

and it ends with a summary:

```
Summary: 9 completed, 0 skipped, 1 failed
Failures:
  123456789  conversions  400 Please remove sessionKeyEventRate to make the request compatible with eventName.
```

The exit code is `0` when every bucket succeeded and `1` if any failed.

## Output

```
export/
  123456789/
    daily_totals.csv
    acquisition.csv
    geo_device.csv
    pages.csv
    conversions.csv
  987654321/
    ...
```

Each CSV has a header row. The first column is always `date`, in GA4's native `YYYYMMDD` form, followed by the bucket's other dimensions and then its metrics, in the order they appear in the YAML. Values are written exactly as the API returns them (strings), so numbers are not rounded or reformatted.

A bucket with no matching data still produces a CSV with only the header row.

## Buckets

`buckets.yaml` lists the reports to run. Each bucket has a name (used as the file name), a list of dimensions, and a list of metrics:

```yaml
buckets:
  - name: acquisition
    dimensions: [sessionSource, sessionMedium, sessionCampaignName, sessionDefaultChannelGroup]
    metrics: [sessions, totalUsers, newUsers, engagedSessions, keyEvents]
```

Rules:

- `date` is added automatically as the first dimension. You do not need to list it, and listing it is harmless.
- The GA4 Data API allows at most **9 dimensions** per request, so a bucket can list up to **8** of its own, and at most **10 metrics**. The tool checks this before making any API calls.
- Bucket names must be unique and are used as file names, so they may only contain letters, digits, `_`, `-` and `.`, and may not start with `.`.
- A bucket may not list the same dimension or metric twice.
- Dimension and metric names are the API names from the [GA4 dimensions and metrics reference](https://developers.google.com/analytics/devguides/reporting/data/v1/api-schema). GA4 has no "goals"; the equivalent is key events (`keyEvents`, `sessionKeyEventRate`, and so on).

Not every combination of dimensions and metrics is valid together. When the API rejects a bucket, the tool logs the full error, records the bucket as failed, and moves on. The error message names the offending field, so fix the YAML and rerun.

The shipped file defines five buckets: daily totals, acquisition, geography and device, pages, and conversions. Edit it freely or point `--config` at your own.

## Resuming and re-running

Before fetching a bucket, the tool checks whether its CSV already exists. If so it logs `skipped` and moves on. This makes a rerun cheap:

- If a run is interrupted, run the same command again. Finished buckets are skipped; unfinished ones are fetched.
- If a bucket failed, fix the cause and run the same command again. Only the failed buckets are attempted.
- To re-export a bucket, delete its CSV first.

While a bucket is being fetched its data goes to a `.csv.part` file, which is renamed to `.csv` only after the last page is written. A crash never leaves a partial file that a later run would mistake for complete.

Because the skip check only looks at the file name, running with a different date range against the same `--out` directory will skip everything. Use a separate `--out` per date range if you export in chunks.

## Quotas and errors

- On quota or rate-limit errors (HTTP 429, "exhausted property tokens"), server errors (500, 503) and request timeouts, the tool waits and retries the page with exponential backoff, starting around 5 seconds and growing to at most 5 minutes between attempts, for up to 15 minutes per page. If the error persists past that, the bucket is recorded as failed and the run continues.
- Each request is given 5 minutes to complete. The GA4 client's built-in default of 60 seconds is too short for a full page of a large report.
- Any other API error for a bucket (invalid or incompatible dimensions, no access to the property, and so on) is logged in full and the bucket is skipped. The run never aborts because of one bucket.
- GA4 keeps processing data for a day or two after it arrives. The tool refuses an `--end-date` in the future and warns when it is within the last 48 hours. If a report changes while its pages are being fetched (the row count differs between pages), the bucket is recorded as failed rather than written with gaps or duplicates; rerun it once the data has settled.
- High-cardinality buckets such as `pages` and `geo_device` over long date ranges can be large. GA4 may collapse rare rows into an `(other)` row when a property exceeds its cardinality limits; this is API behaviour, not something the tool can avoid. Shorter date ranges reduce it.

## Development

```sh
pip install -r requirements-dev.txt
pytest
```

The tests replace the GA4 clients with in-memory fakes, so they need no network or credentials. GitHub Actions runs them on every pull request.
