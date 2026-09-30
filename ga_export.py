#!/usr/bin/env python3
"""Export historical GA4 report data to CSV, one file per property per bucket."""

import argparse
import csv
import logging
import os
import re
import sys
import warnings
from datetime import date, datetime, timedelta

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
from google.api_core import retry as gretry
from google.auth.exceptions import GoogleAuthError

PAGE_LIMIT = 250000  # documented maximum rows per runReport request
MAX_DIMENSIONS = 9  # GA4 Data API limit per request, including the implicit "date"
MAX_METRICS = 10
BUCKET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

# The generated client gives run_report a 60s timeout and no retry. A full page can take
# longer than that, and quota and 5xx errors are routine, so every page is sent with an
# explicit per-attempt timeout and a jittered exponential-backoff retry policy.
REQUEST_TIMEOUT_SECONDS = 300
RETRY_INITIAL_SECONDS = 5
RETRY_MAX_SLEEP_SECONDS = 300
RETRY_DEADLINE_SECONDS = 900
RETRYABLE_ERRORS = (
    gexc.TooManyRequests,  # 429; ResourceExhausted (quota) is a subclass
    gexc.ServiceUnavailable,
    gexc.InternalServerError,
    gexc.DeadlineExceeded,
)

log = logging.getLogger("ga_export")


class InconsistentReport(Exception):
    """The report changed between pages, so the pages cannot be stitched into one file."""


def today():
    return date.today()


# ---------------------------------------------------------------- list-properties


def list_properties(args):
    client = AnalyticsAdminServiceClient()
    try:
        summaries = list(client.list_account_summaries())
    except gexc.GoogleAPICallError as e:
        sys.exit(f"Could not list properties: {e}")
    rows = []
    for summary in summaries:
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
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        sys.exit(f"Config file not found: {path}")
    except yaml.YAMLError as e:
        sys.exit(f"Could not parse {path}: {e}")

    buckets = data.get("buckets") if isinstance(data, dict) else None
    if not isinstance(buckets, list) or not buckets:
        sys.exit(f"{path}: expected a non-empty top-level 'buckets' list")

    out, seen = [], set()
    for i, b in enumerate(buckets, start=1):
        if not isinstance(b, dict) or not isinstance(b.get("name"), str) or not b["name"]:
            sys.exit(f"{path}: bucket #{i} needs a non-empty string 'name'")
        name = b["name"]
        if name in seen:
            sys.exit(f"{path}: duplicate bucket name '{name}'")
        if not BUCKET_NAME_RE.match(name) or name in (".", ".."):
            sys.exit(
                f"{path}: bucket name '{name}' is not a valid file name "
                "(use letters, digits, '_', '-' and '.')"
            )
        seen.add(name)

        dims = b.get("dimensions") or []
        mets = b.get("metrics") or []
        if not isinstance(dims, list) or not all(isinstance(d, str) for d in dims):
            sys.exit(f"{path}: bucket '{name}': 'dimensions' must be a list of strings")
        if not isinstance(mets, list) or not mets or not all(isinstance(m, str) for m in mets):
            sys.exit(f"{path}: bucket '{name}': 'metrics' must be a non-empty list of strings")
        if len(set(dims)) != len(dims):
            sys.exit(f"{path}: bucket '{name}': 'dimensions' contains a duplicate")
        if len(set(mets)) != len(mets):
            sys.exit(f"{path}: bucket '{name}': 'metrics' contains a duplicate")

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


def describe(e):
    """One-line description of an error. str() on an API error appends the multi-line
    gRPC details block, which is noise in a progress log and a summary."""
    if isinstance(e, gexc.GoogleAPICallError):
        text = " ".join(str(e.message).split())
        if e.code:
            text = f"{int(e.code)} {text}"
        return f"{text} [{e.reason}]" if e.reason else text
    if isinstance(e, gexc.RetryError):
        return f"{e.message}, last exception: {describe(e.cause)}"
    return str(e)


def retry_policy(label):
    """Retry policy for one bucket: retries quota, 5xx and timeout errors with jittered
    exponential backoff (up to RETRY_MAX_SLEEP_SECONDS between attempts) for up to
    RETRY_DEADLINE_SECONDS, then raises google.api_core.exceptions.RetryError."""

    def on_error(exc):
        log.warning("%s transient error (%s); retrying", label, describe(exc))

    return gretry.Retry(
        predicate=gretry.if_exception_type(*RETRYABLE_ERRORS),
        initial=RETRY_INITIAL_SECONDS,
        maximum=RETRY_MAX_SLEEP_SECONDS,
        multiplier=2,
        timeout=RETRY_DEADLINE_SECONDS,
        on_error=on_error,
    )


def export_bucket(client, property_id, bucket, start_date, end_date, csv_path, label):
    """Stream every page of the report into csv_path + '.part', then rename. Returns row count."""
    part_path = csv_path + ".part"
    retry = retry_policy(label)
    offset = 0
    total = None
    with open(part_path, "w", newline="", encoding="utf-8") as f:
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
            response = client.run_report(request, retry=retry, timeout=REQUEST_TIMEOUT_SECONDS)
            if total is None:
                total = response.row_count
                writer.writerow(
                    [h.name for h in response.dimension_headers]
                    + [h.name for h in response.metric_headers]
                )
            elif response.row_count != total:
                raise InconsistentReport(
                    f"row count changed from {total} to {response.row_count} between pages; "
                    "the report is still changing (is --end-date too recent?)"
                )
            for row in response.rows:
                writer.writerow(
                    [v.value for v in row.dimension_values]
                    + [v.value for v in row.metric_values]
                )
            page_rows = len(response.rows)
            offset += page_rows
            log.info("%s fetched %d/%d rows", label, offset, total)
            if page_rows == 0 or offset >= total:
                break
    os.replace(part_path, csv_path)
    return offset


def parse_date(value, flag):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        sys.exit(f"{flag} must be YYYY-MM-DD, got '{value}'")


def parse_property_ids(value):
    """Accept '123' or 'properties/123', comma-separated. Drops duplicates, rejects junk."""
    ids = []
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        pid = token[len("properties/"):] if token.startswith("properties/") else token
        if not pid.isdigit():
            sys.exit(
                f"--properties: '{token}' is not a GA4 property ID "
                "(expected digits, or properties/<digits>)"
            )
        if pid in ids:
            log.warning("--properties: ignoring duplicate property ID %s", pid)
            continue
        ids.append(pid)
    if not ids:
        sys.exit("--properties must contain at least one property ID")
    return ids


def extract(args):
    start = parse_date(args.start_date, "--start-date")
    end = parse_date(args.end_date, "--end-date")
    if start > end:
        sys.exit(f"--start-date {args.start_date} is after --end-date {args.end_date}")
    if end > today():
        sys.exit(f"--end-date {args.end_date} is in the future")
    if end >= today() - timedelta(days=2):
        log.warning(
            "--end-date %s is within the last 48 hours; GA4 may still be processing that data, "
            "so the most recent days can be incomplete",
            args.end_date,
        )

    property_ids = parse_property_ids(args.properties)
    buckets = load_buckets(args.config)
    client = BetaAnalyticsDataClient()

    completed = skipped = failed = 0
    failures = []
    for pid in property_ids:
        prop_dir = os.path.join(args.out, pid)
        os.makedirs(prop_dir, exist_ok=True)
        for i, bucket in enumerate(buckets):
            label = f"[{pid}/{bucket['name']}]"
            csv_path = os.path.join(prop_dir, f"{bucket['name']}.csv")
            if os.path.exists(csv_path):
                log.info("%s skipped (already exists: %s)", label, csv_path)
                skipped += 1
                continue
            log.info("%s starting (%s to %s)", label, args.start_date, args.end_date)
            try:
                n = export_bucket(client, pid, bucket, args.start_date, args.end_date, csv_path, label)
            except (gexc.GoogleAPICallError, gexc.RetryError, InconsistentReport) as e:
                message = describe(e)
                log.error("%s failed: %s", label, message)
                failures.append((pid, bucket["name"], message))
                failed += 1
                if os.path.exists(csv_path + ".part"):
                    os.remove(csv_path + ".part")
                remaining = [b["name"] for b in buckets[i + 1:]]
                if isinstance(e, gexc.Forbidden) and remaining:
                    # A 403 is about the property, not the bucket: every other bucket
                    # would fail the same way, so don't ask.
                    log.error(
                        "[%s] no access to this property; not attempting its remaining %d bucket(s)",
                        pid, len(remaining),
                    )
                    failures.append((pid, ", ".join(remaining), f"not attempted: no access to property {pid}"))
                    failed += len(remaining)
                    break
                continue
            log.info("%s done: %d rows -> %s", label, n, csv_path)
            completed += 1

    print()
    print(f"Summary: {completed} completed, {skipped} skipped, {failed} failed")
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
    p_ext.add_argument("--end-date", required=True, help="YYYY-MM-DD, not in the future")
    p_ext.add_argument("--config", default="buckets.yaml", help="Bucket YAML file (default: buckets.yaml)")
    p_ext.add_argument("--out", default="./export", help="Output directory (default: ./export)")
    p_ext.set_defaults(func=extract)

    args = parser.parse_args()
    logging.basicConfig(
        stream=sys.stdout, level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        return args.func(args)
    except GoogleAuthError as e:
        sys.exit(f"Google credentials problem (check GOOGLE_APPLICATION_CREDENTIALS): {e}")


if __name__ == "__main__":
    sys.exit(main())
