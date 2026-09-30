#!/usr/bin/env python3
"""Export historical GA4 report data to CSV, one file per property per bucket."""

import argparse
import csv
import logging
import os
import sys
import time
import warnings
from datetime import datetime

# The Google client libraries emit Python-version and OpenSSL deprecation warnings on
# import that would otherwise flood a long-running export log.
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*OpenSSL.*")

import yaml  # noqa: E402
from google.analytics.admin_v1beta import AnalyticsAdminServiceClient
from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    OrderBy,
    RunReportRequest,
)
from google.api_core import exceptions as gexc
from google.auth.exceptions import DefaultCredentialsError

PAGE_LIMIT = 100000
MAX_DIMENSIONS = 9  # GA4 Data API limit per request, including the implicit "date"
MAX_METRICS = 10
MAX_RETRIES = 8
MAX_BACKOFF_SECONDS = 300

log = logging.getLogger("ga_export")


# ---------------------------------------------------------------- list-properties


def list_properties(args):
    client = AnalyticsAdminServiceClient()
    rows = []
    for summary in client.list_account_summaries():
        for prop in summary.property_summaries:
            rows.append(
                (summary.display_name, prop.property.split("/")[-1], prop.display_name)
            )
    if not rows:
        print("No accessible properties found.")
        return 0
    header = ("ACCOUNT", "PROPERTY_ID", "PROPERTY_NAME")
    widths = [max(len(r[i]) for r in rows + [header]) for i in range(3)]
    for r in [header] + rows:
        print("  ".join(r[i].ljust(widths[i]) for i in range(3)).rstrip())
    return 0


# ---------------------------------------------------------------- config


def load_buckets(path):
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f"Config file not found: {path}")
    except yaml.YAMLError as e:
        sys.exit(f"Could not parse {path}: {e}")

    buckets = (data or {}).get("buckets") if isinstance(data, dict) else None
    if not isinstance(buckets, list) or not buckets:
        sys.exit(f"{path}: expected a non-empty top-level 'buckets' list")

    out, seen = [], set()
    for i, b in enumerate(buckets, start=1):
        if not isinstance(b, dict) or not isinstance(b.get("name"), str) or not b["name"]:
            sys.exit(f"{path}: bucket #{i} needs a non-empty string 'name'")
        name = b["name"]
        if name in seen:
            sys.exit(f"{path}: duplicate bucket name '{name}'")
        if os.sep in name or name in (".", ".."):
            sys.exit(f"{path}: bucket name '{name}' is not a valid file name")
        seen.add(name)

        dims = b.get("dimensions") or []
        mets = b.get("metrics") or []
        if not isinstance(dims, list) or not all(isinstance(d, str) for d in dims):
            sys.exit(f"{path}: bucket '{name}': 'dimensions' must be a list of strings")
        if not isinstance(mets, list) or not mets or not all(isinstance(m, str) for m in mets):
            sys.exit(f"{path}: bucket '{name}': 'metrics' must be a non-empty list of strings")

        dims = ["date"] + [d for d in dims if d != "date"]
        if len(dims) > MAX_DIMENSIONS:
            sys.exit(
                f"{path}: bucket '{name}' has {len(dims)} dimensions including 'date'; "
                f"the GA4 Data API allows at most {MAX_DIMENSIONS}"
            )
        if len(mets) > MAX_METRICS:
            sys.exit(
                f"{path}: bucket '{name}' has {len(mets)} metrics; "
                f"the GA4 Data API allows at most {MAX_METRICS}"
            )
        out.append({"name": name, "dimensions": dims, "metrics": mets})
    return out


# ---------------------------------------------------------------- extract


def run_with_retry(client, request, label):
    attempt = 0
    while True:
        try:
            return client.run_report(request)
        except (gexc.ResourceExhausted, gexc.TooManyRequests) as e:
            if attempt >= MAX_RETRIES:
                log.error("%s giving up after %d retries on quota/rate limit", label, MAX_RETRIES)
                raise
            wait = min(MAX_BACKOFF_SECONDS, 5 * 2 ** attempt)
            attempt += 1
            log.warning(
                "%s quota/rate limit hit (%s); retry %d/%d in %ds",
                label, e.message, attempt, MAX_RETRIES, wait,
            )
            time.sleep(wait)


def export_bucket(client, property_id, bucket, start_date, end_date, csv_path, label):
    """Stream every page of the report into csv_path + '.part', then rename. Returns row count."""
    part_path = csv_path + ".part"
    offset = 0
    with open(part_path, "w", newline="") as f:
        writer = csv.writer(f)
        while True:
            request = RunReportRequest(
                property=f"properties/{property_id}",
                dimensions=[Dimension(name=d) for d in bucket["dimensions"]],
                metrics=[Metric(name=m) for m in bucket["metrics"]],
                date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
                order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))],
                limit=PAGE_LIMIT,
                offset=offset,
            )
            response = run_with_retry(client, request, label)
            if offset == 0:
                writer.writerow(
                    [h.name for h in response.dimension_headers]
                    + [h.name for h in response.metric_headers]
                )
            for row in response.rows:
                writer.writerow(
                    [v.value for v in row.dimension_values]
                    + [v.value for v in row.metric_values]
                )
            page_rows = len(response.rows)
            offset += page_rows
            log.info("%s fetched %d/%d rows", label, offset, response.row_count)
            if page_rows == 0 or offset >= response.row_count:
                break
    os.replace(part_path, csv_path)
    return offset


def parse_date(value, flag):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        sys.exit(f"{flag} must be YYYY-MM-DD, got '{value}'")


def extract(args):
    start = parse_date(args.start_date, "--start-date")
    end = parse_date(args.end_date, "--end-date")
    if start > end:
        sys.exit(f"--start-date {args.start_date} is after --end-date {args.end_date}")

    property_ids = [p.strip().split("/")[-1] for p in args.properties.split(",") if p.strip()]
    if not property_ids:
        sys.exit("--properties must contain at least one property ID")

    buckets = load_buckets(args.config)
    client = BetaAnalyticsDataClient()

    completed = skipped = 0
    failures = []
    for pid in property_ids:
        prop_dir = os.path.join(args.out, pid)
        os.makedirs(prop_dir, exist_ok=True)
        for bucket in buckets:
            label = f"[{pid}/{bucket['name']}]"
            csv_path = os.path.join(prop_dir, f"{bucket['name']}.csv")
            if os.path.exists(csv_path):
                log.info("%s skipped (already exists: %s)", label, csv_path)
                skipped += 1
                continue
            log.info("%s starting (%s to %s)", label, args.start_date, args.end_date)
            try:
                n = export_bucket(client, pid, bucket, args.start_date, args.end_date, csv_path, label)
            except gexc.GoogleAPICallError as e:
                log.error("%s failed: %s", label, e)
                failures.append((pid, bucket["name"], str(e)))
                if os.path.exists(csv_path + ".part"):
                    os.remove(csv_path + ".part")
                continue
            log.info("%s done: %d rows -> %s", label, n, csv_path)
            completed += 1

    print()
    print(f"Summary: {completed} completed, {skipped} skipped, {len(failures)} failed")
    if failures:
        print("Failures:")
        for pid, name, msg in failures:
            print(f"  {pid}  {name}  {msg}")
    return 1 if failures else 0


# ---------------------------------------------------------------- main


def main():
    parser = argparse.ArgumentParser(description="Export historical GA4 report data to CSV.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-properties", help="List every accessible GA4 property")
    p_list.set_defaults(func=list_properties)

    p_ext = sub.add_parser("extract", help="Export report buckets to CSV")
    p_ext.add_argument("--properties", required=True, help="Comma-separated GA4 property IDs")
    p_ext.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    p_ext.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    p_ext.add_argument("--config", default="buckets.yaml", help="Bucket YAML file (default: buckets.yaml)")
    p_ext.add_argument("--out", default="./export", help="Output directory (default: ./export)")
    p_ext.set_defaults(func=extract)

    args = parser.parse_args()
    logging.basicConfig(
        stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        return args.func(args)
    except DefaultCredentialsError as e:
        sys.exit(f"Could not load Google credentials (set GOOGLE_APPLICATION_CREDENTIALS): {e}")


if __name__ == "__main__":
    sys.exit(main())
