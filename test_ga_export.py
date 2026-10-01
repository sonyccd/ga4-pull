"""Unit tests for ga_export.py. No network: the GA4 clients are replaced with fakes."""

import csv
import functools
import os
import random
import sys
import time
from datetime import date, timedelta
from types import SimpleNamespace as NS

import pytest
from google.api_core import exceptions as gexc
from google.api_core import retry as gretry
from google.auth.exceptions import DefaultCredentialsError, RefreshError
from google.rpc import error_details_pb2

import ga_export

HERE = os.path.dirname(os.path.abspath(__file__))
TODAY = date(2026, 9, 30)


# ---------------------------------------------------------------- helpers


def fake_quota(cost=12, project_hour=139_000, hour=390_000, day=1_900_000):
    return NS(
        tokens_per_day=NS(consumed=cost, remaining=day),
        tokens_per_hour=NS(consumed=cost, remaining=hour),
        tokens_per_project_per_hour=NS(consumed=cost, remaining=project_hour),
    )


def fake_response(dims, mets, rows, row_count, quota=None):
    """Mimic the parts of RunReportResponse that ga_export reads."""
    return NS(
        dimension_headers=[NS(name=d) for d in dims],
        metric_headers=[NS(name=m) for m in mets],
        rows=[
            NS(
                dimension_values=[NS(value=v) for v in r[: len(dims)]],
                metric_values=[NS(value=v) for v in r[len(dims):]],
            )
            for r in rows
        ],
        row_count=row_count,
        property_quota=quota or fake_quota(),
    )


HOURLY_QUOTA_MSG = (
    "Exhausted property tokens for a project per hour. These quota tokens will return in "
    "under an hour. To learn more, see https://developers.google.com/analytics/devguides/reporting/data/v1/quotas"
)
DAILY_QUOTA_MSG = (
    "Exhausted property tokens for a property per day. These quota tokens will return at "
    "midnight Pacific time. To learn more, see https://developers.google.com/analytics/devguides/reporting/data/v1/quotas"
)


def hourly_quota_error():
    return gexc.ResourceExhausted(HOURLY_QUOTA_MSG)


def daily_quota_error():
    return gexc.ResourceExhausted(DAILY_QUOTA_MSG)


class FakeClient:
    """Base for fake Data API clients. Applies the retry policy the way the real
    generated client does, so retry behaviour is exercised end to end."""

    def __init__(self):
        self.calls = 0
        self.requests = []
        self.kwargs = []

    def run_report(self, request, retry=None, timeout=None):
        self.kwargs.append({"retry": retry, "timeout": timeout})
        attempt = functools.partial(self._attempt, request)
        return retry(attempt)() if retry is not None else attempt()

    def _attempt(self, request):
        self.calls += 1
        self.requests.append(request)
        return self.respond(request)


class PagedClient(FakeClient):
    """Serves `all_rows` in pages of `request.limit`."""

    def __init__(self, all_rows, dims=("date",), mets=("sessions",)):
        super().__init__()
        self.all_rows = all_rows
        self.dims, self.mets = list(dims), list(mets)

    def respond(self, request):
        page = self.all_rows[request.offset : request.offset + request.limit]
        return fake_response(self.dims, self.mets, page, len(self.all_rows))


class ScriptedClient(FakeClient):
    """Returns or raises each scripted item in turn."""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)

    def respond(self, request):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class RoutingClient(FakeClient):
    """Behaviour depends on which non-date dimension the bucket asks for."""

    def respond(self, request):
        dims = [d.name for d in request.dimensions]
        mets = [m.name for m in request.metrics]
        if request.property == "properties/403":
            raise make_403("PERMISSION_DENIED")
        if "bad" in dims:
            raise gexc.InvalidArgument("Please remove bad to make the request compatible")
        if "quota" in dims:
            raise hourly_quota_error()
        if "daily" in dims:
            raise daily_quota_error()
        if "flaky" in dims and self.calls == 1:
            raise gexc.ServiceUnavailable("try again")
        if "shifting" in dims:
            # Two rows in total, one per page, but the total changes between pages.
            return fake_response(dims, mets, [["20240101"] + ["s"] * (len(dims) - 1) + ["1"] * len(mets)],
                                 2 + request.offset)
        return fake_response(dims, mets, [["20240101"] + ["x"] * (len(dims) - 1) + ["1"] * len(mets)], 1)


def make_403(reason=None):
    """A PermissionDenied shaped like the one the gRPC transport builds from a real 403."""
    info = error_details_pb2.ErrorInfo(reason=reason, domain="analyticsdata.googleapis.com") if reason else None
    return gexc.PermissionDenied(
        "User does not have sufficient permissions for this property.",
        details=[info] if info else (),
        error_info=info,
    )


def write_yaml(tmp_path, text):
    p = tmp_path / "buckets.yaml"
    p.write_text(text)
    return str(p)


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


@pytest.fixture
def fake_clock(monkeypatch):
    """Make sleeps instant and deterministic: no jitter, and time.monotonic advances by
    exactly the slept amount so retry deadlines are reached on schedule."""
    clock = {"now": 1000.0, "sleeps": []}

    def sleep(seconds):
        clock["sleeps"].append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(random, "uniform", lambda low, high: high)
    return clock["sleeps"]


@pytest.fixture
def fixed_today(monkeypatch):
    monkeypatch.setattr(ga_export, "today", lambda: TODAY)
    return TODAY


# ---------------------------------------------------------------- load_buckets


def test_load_buckets_shipped_config_is_valid():
    buckets = ga_export.load_buckets(os.path.join(HERE, "buckets.yaml"))
    assert [b["name"] for b in buckets] == [
        "daily_totals", "acquisition", "geo_device", "pages", "conversions",
    ]
    for b in buckets:
        assert b["dimensions"][0] == "date"
        assert len(b["dimensions"]) <= ga_export.MAX_DIMENSIONS
        assert 0 < len(b["metrics"]) <= ga_export.MAX_METRICS


def test_load_buckets_prepends_date_and_dedupes(tmp_path):
    path = write_yaml(tmp_path, "buckets:\n  - name: a\n    dimensions: [country, date]\n    metrics: [sessions]\n")
    [b] = ga_export.load_buckets(path)
    assert b["dimensions"] == ["date", "country"]
    assert b["metrics"] == ["sessions"]


def test_load_buckets_missing_dimensions_key_defaults_to_date_only(tmp_path):
    path = write_yaml(tmp_path, "buckets:\n  - name: a\n    metrics: [sessions]\n")
    [b] = ga_export.load_buckets(path)
    assert b["dimensions"] == ["date"]


@pytest.mark.parametrize("name", ["daily_totals", "geo-device", "pages.2024", "A1"])
def test_load_buckets_accepts_safe_names(tmp_path, name):
    path = write_yaml(tmp_path, f"buckets:\n  - name: '{name}'\n    metrics: [sessions]\n")
    [b] = ga_export.load_buckets(path)
    assert b["name"] == name


@pytest.mark.parametrize(
    "text, message",
    [
        ("", "expected a non-empty top-level 'buckets' list"),
        ("buckets: []", "expected a non-empty top-level 'buckets' list"),
        ("buckets: notalist", "expected a non-empty top-level 'buckets' list"),
        ("- just\n- a list\n", "expected a non-empty top-level 'buckets' list"),
        ("buckets:\n  - dimensions: []\n    metrics: [x]\n", "bucket #1 needs a non-empty string 'name'"),
        ("buckets:\n  - name: ''\n    metrics: [x]\n", "bucket #1 needs a non-empty string 'name'"),
        ("buckets:\n  - name: a\n    metrics: [x]\n  - name: a\n    metrics: [x]\n", "duplicate bucket name 'a'"),
        ("buckets:\n  - name: ../evil\n    metrics: [x]\n", "not a valid file name"),
        ("buckets:\n  - name: 'a/b'\n    metrics: [x]\n", "not a valid file name"),
        ("buckets:\n  - name: 'a\\\\b'\n    metrics: [x]\n", "not a valid file name"),
        ("buckets:\n  - name: 'a b'\n    metrics: [x]\n", "not a valid file name"),
        ("buckets:\n  - name: 'a:b'\n    metrics: [x]\n", "not a valid file name"),
        ("buckets:\n  - name: '.hidden'\n    metrics: [x]\n", "not a valid file name"),
        ("buckets:\n  - name: '..'\n    metrics: [x]\n", "not a valid file name"),
        ("buckets:\n  - name: a\n    dimensions: country\n    metrics: [x]\n", "'dimensions' must be a list of strings"),
        ("buckets:\n  - name: a\n    dimensions: [1]\n    metrics: [x]\n", "'dimensions' must be a list of strings"),
        ("buckets:\n  - name: a\n    dimensions: []\n", "'metrics' must be a non-empty list of strings"),
        ("buckets:\n  - name: a\n    metrics: []\n", "'metrics' must be a non-empty list of strings"),
        ("buckets:\n  - name: a\n    metrics: sessions\n", "'metrics' must be a non-empty list of strings"),
        ("buckets:\n  - name: a\n    dimensions: [country, country]\n    metrics: [x]\n", "'dimensions' contains a duplicate"),
        ("buckets:\n  - name: a\n    metrics: [sessions, sessions]\n", "'metrics' contains a duplicate"),
        ("buckets:\n  - name: a\n    dimensions: [a,b,c,d,e,f,g,h,i]\n    metrics: [x]\n", "has 10 dimensions including 'date'; the GA4 Data API allows at most 9"),
        ("buckets:\n  - name: a\n    metrics: [a,b,c,d,e,f,g,h,i,j,k]\n", "has 11 metrics; the GA4 Data API allows at most 10"),
        ("buckets: [\n", "Could not parse"),
    ],
)
def test_load_buckets_rejects_bad_config(tmp_path, text, message):
    path = write_yaml(tmp_path, text)
    with pytest.raises(SystemExit) as exc:
        ga_export.load_buckets(path)
    assert message in str(exc.value)


def test_load_buckets_nine_dimensions_including_date_is_allowed(tmp_path):
    path = write_yaml(tmp_path, "buckets:\n  - name: a\n    dimensions: [a,b,c,d,e,f,g,h]\n    metrics: [x]\n")
    [b] = ga_export.load_buckets(path)
    assert len(b["dimensions"]) == 9


def test_load_buckets_missing_file(tmp_path):
    with pytest.raises(SystemExit, match="Config file not found"):
        ga_export.load_buckets(str(tmp_path / "nope.yaml"))


# ---------------------------------------------------------------- parse_date / parse_property_ids


def test_parse_date_valid():
    assert ga_export.parse_date("2024-02-29", "--start-date").isoformat() == "2024-02-29"


@pytest.mark.parametrize("value", ["2024-13-01", "2023-02-29", "01/02/2024", "20240101", ""])
def test_parse_date_invalid(value):
    with pytest.raises(SystemExit, match="--end-date must be YYYY-MM-DD"):
        ga_export.parse_date(value, "--end-date")


def test_parse_property_ids_accepts_both_forms_and_whitespace():
    assert ga_export.parse_property_ids(" 123 , properties/456,,789") == ["123", "456", "789"]


def test_parse_property_ids_drops_duplicates_with_warning(caplog):
    with caplog.at_level("WARNING", logger="ga_export"):
        assert ga_export.parse_property_ids("123,properties/123,456") == ["123", "456"]
    assert "ignoring duplicate property ID 123" in caplog.text


@pytest.mark.parametrize("value", ["properties/", "123/", "abc", "properties/abc", "12 3", "-1"])
def test_parse_property_ids_rejects_junk(value):
    with pytest.raises(SystemExit, match="is not a GA4 property ID"):
        ga_export.parse_property_ids(value)


@pytest.mark.parametrize("value", ["", " , ", ","])
def test_parse_property_ids_requires_at_least_one(value):
    with pytest.raises(SystemExit, match="at least one property ID"):
        ga_export.parse_property_ids(value)


# ---------------------------------------------------------------- transient_policy


def call_with_transient_policy(client, request="req"):
    return client.run_report(request, retry=ga_export.transient_policy("[l]"))


def test_transient_policy_returns_immediately_on_success(fake_clock):
    client = ScriptedClient(["ok"])
    assert call_with_transient_policy(client) == "ok"
    assert client.calls == 1
    assert fake_clock == []


def test_transient_policy_retries_5xx_and_timeouts_with_exponential_backoff(fake_clock):
    client = ScriptedClient([
        gexc.ServiceUnavailable("503"),
        gexc.InternalServerError("500"),
        gexc.DeadlineExceeded("timeout"),
        gexc.ServiceUnavailable("502:Bad Gateway"),
        "ok",
    ])
    assert call_with_transient_policy(client) == "ok"
    assert client.calls == 5
    assert fake_clock == [5, 10, 20, 40]


def test_transient_policy_caps_sleep_and_gives_up_at_deadline(fake_clock, caplog):
    client = ScriptedClient([gexc.ServiceUnavailable("503")] * 50)
    with caplog.at_level("WARNING", logger="ga_export"), pytest.raises(gexc.RetryError) as exc:
        call_with_transient_policy(client)
    assert isinstance(exc.value.cause, gexc.ServiceUnavailable)
    assert "Timeout of 900.0s exceeded" in str(exc.value)
    assert fake_clock == [5, 10, 20, 40, 80, 160, 300]
    assert sum(fake_clock) <= ga_export.RETRY_DEADLINE_SECONDS
    assert client.calls == len(fake_clock) + 1
    assert caplog.text.count("transient error (503 503); retrying") == client.calls


@pytest.mark.parametrize("error", [
    gexc.InvalidArgument("bad dimension"),
    gexc.PermissionDenied("no access"),
    gexc.NotFound("no such property"),
    hourly_quota_error(),  # quota exhaustion is not transient; quota_policy owns it
    daily_quota_error(),
])
def test_transient_policy_does_not_retry_other_errors(fake_clock, error):
    client = ScriptedClient([error, "never reached"])
    with pytest.raises(type(error)):
        call_with_transient_policy(client)
    assert client.calls == 1
    assert fake_clock == []


# ---------------------------------------------------------------- quota_policy / fetch_page


def test_is_daily_quota_error_distinguishes_daily_from_hourly():
    assert ga_export.is_daily_quota_error(daily_quota_error())
    assert not ga_export.is_daily_quota_error(hourly_quota_error())
    assert not ga_export.is_daily_quota_error(gexc.ServiceUnavailable("day of reckoning"))


def test_fetch_page_waits_out_hourly_quota_then_succeeds(fake_clock, caplog):
    client = ScriptedClient([hourly_quota_error(), hourly_quota_error(), hourly_quota_error(), "ok"])
    with caplog.at_level("WARNING", logger="ga_export"):
        assert ga_export.fetch_page(client, "req", "[l]") == "ok"
    assert client.calls == 4
    assert fake_clock == [60, 120, 240]
    assert caplog.text.count("quota exhausted") == 3
    assert "waiting for the hourly quota to refresh" in caplog.text


def test_fetch_page_polls_for_about_an_hour_before_giving_up(fake_clock):
    client = ScriptedClient([hourly_quota_error()] * 100)
    with pytest.raises(gexc.RetryError) as exc:
        ga_export.fetch_page(client, "req", "[l]")
    assert isinstance(exc.value.cause, gexc.ResourceExhausted)
    assert "Timeout of 3900.0s exceeded" in str(exc.value)
    assert fake_clock[:4] == [60, 120, 240, 300]
    assert max(fake_clock) == ga_export.QUOTA_POLL_MAX_SECONDS == 300
    assert 3600 <= sum(fake_clock) <= ga_export.QUOTA_WAIT_SECONDS


def test_fetch_page_does_not_wait_for_daily_quota(fake_clock):
    client = ScriptedClient([daily_quota_error(), "never reached"])
    with pytest.raises(gexc.ResourceExhausted, match="per day"):
        ga_export.fetch_page(client, "req", "[l]")
    assert client.calls == 1
    assert fake_clock == []


def test_fetch_page_composes_transient_retry_inside_quota_wait(fake_clock):
    client = ScriptedClient([
        gexc.ServiceUnavailable("503"),  # transient: 5s
        hourly_quota_error(),            # quota: 60s, then a fresh transient budget
        gexc.ServiceUnavailable("503"),  # transient again: 5s
        "ok",
    ])
    assert ga_export.fetch_page(client, "req", "[l]") == "ok"
    assert client.calls == 4
    assert fake_clock == [5, 60, 5]
    assert all(kw["timeout"] == ga_export.REQUEST_TIMEOUT_SECONDS for kw in client.kwargs)
    assert all(isinstance(kw["retry"], gretry.Retry) for kw in client.kwargs)


def test_fetch_page_does_not_retry_permanent_errors(fake_clock):
    client = ScriptedClient([gexc.InvalidArgument("bad"), "never reached"])
    with pytest.raises(gexc.InvalidArgument):
        ga_export.fetch_page(client, "req", "[l]")
    assert client.calls == 1
    assert fake_clock == []


# ---------------------------------------------------------------- describe


def test_describe_api_error_is_one_line_with_code_and_reason():
    text = ga_export.describe(make_403("PERMISSION_DENIED"))
    assert text == "403 User does not have sufficient permissions for this property. [PERMISSION_DENIED]"
    assert "\n" not in text


def test_describe_api_error_without_error_info():
    assert ga_export.describe(gexc.InvalidArgument("bad dimension")) == "400 bad dimension"


def test_describe_collapses_multiline_messages():
    assert ga_export.describe(gexc.InvalidArgument("line one\n  line two")) == "400 line one line two"


def test_describe_retry_error_names_the_last_cause():
    err = gexc.RetryError("Timeout of 900.0s exceeded", gexc.ResourceExhausted("tokens", error_info=error_details_pb2.ErrorInfo(reason="RATE_LIMIT_EXCEEDED")))
    assert ga_export.describe(err) == "Timeout of 900.0s exceeded, last exception: 429 tokens [RATE_LIMIT_EXCEEDED]"


def test_describe_other_exceptions_use_str():
    assert ga_export.describe(ga_export.InconsistentReport("row count changed")) == "row count changed"


# ---------------------------------------------------------------- export_bucket


BUCKET = {"name": "b", "dimensions": ["date", "country"], "metrics": ["sessions", "totalUsers"]}


def run_export(client, tmp_path, bucket=BUCKET, start="2024-01-01", end="2024-01-07"):
    out = tmp_path / "b.csv"
    n = ga_export.export_bucket(client, "123", bucket, start, end, str(out), "[l]")
    return n, out


def test_export_bucket_paginates_and_writes_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(ga_export, "PAGE_LIMIT", 3)
    rows = [[f"2024010{i}", "US", str(i), str(i * 10)] for i in range(1, 8)]
    client = PagedClient(rows, dims=BUCKET["dimensions"], mets=BUCKET["metrics"])

    n, out = run_export(client, tmp_path)

    assert n == 7
    assert read_csv(out) == [["date", "country", "sessions", "totalUsers"]] + rows
    assert not (tmp_path / "b.csv.part").exists()
    assert [r.offset for r in client.requests] == [0, 3, 6]
    assert all(r.limit == 3 for r in client.requests)


def test_export_bucket_stops_when_row_count_is_exact_multiple_of_page(tmp_path, monkeypatch):
    monkeypatch.setattr(ga_export, "PAGE_LIMIT", 3)
    rows = [[f"2024010{i}", "US", "1", "1"] for i in range(1, 7)]
    client = PagedClient(rows, dims=BUCKET["dimensions"], mets=BUCKET["metrics"])
    n, _ = run_export(client, tmp_path)
    assert n == 6
    assert [r.offset for r in client.requests] == [0, 3]


def test_export_bucket_empty_result_writes_header_only(tmp_path):
    client = PagedClient([], dims=BUCKET["dimensions"], mets=BUCKET["metrics"])
    n, out = run_export(client, tmp_path)
    assert n == 0
    assert read_csv(out) == [["date", "country", "sessions", "totalUsers"]]


def test_export_bucket_builds_request_from_bucket_and_dates(tmp_path):
    client = PagedClient([], dims=BUCKET["dimensions"], mets=BUCKET["metrics"])
    run_export(client, tmp_path)
    [req] = client.requests
    assert req.property == "properties/123"
    assert [d.name for d in req.dimensions] == ["date", "country"]
    assert [m.name for m in req.metrics] == ["sessions", "totalUsers"]
    assert req.date_ranges[0].start_date == "2024-01-01"
    assert req.date_ranges[0].end_date == "2024-01-07"
    assert req.order_bys[0].dimension.dimension_name == "date"
    assert req.limit == ga_export.PAGE_LIMIT == 250000
    assert req.offset == 0
    assert req.return_property_quota is True


def test_export_bucket_logs_page_cost_and_remaining_quota(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(ga_export, "PAGE_LIMIT", 1)
    dims, mets = BUCKET["dimensions"], BUCKET["metrics"]
    client = ScriptedClient([
        fake_response(dims, mets, [["20240101", "US", "1", "1"]], 2, quota=fake_quota(cost=1500, project_hour=120_000, hour=380_000, day=1_800_000)),
        fake_response(dims, mets, [["20240102", "US", "1", "1"]], 2, quota=fake_quota(cost=1400, project_hour=118_600, hour=378_600, day=1_798_600)),
    ])
    with caplog.at_level("INFO", logger="ga_export"):
        run_export(client, tmp_path)
    assert "[l] fetched 1/2 rows (page cost 1500 tokens; remaining: 120000 project/hour, 380000 property/hour, 1800000 property/day)" in caplog.text
    assert "[l] fetched 2/2 rows (page cost 1400 tokens; remaining: 118600 project/hour, 378600 property/hour, 1798600 property/day)" in caplog.text


def test_export_bucket_passes_transient_policy_and_timeout_to_every_call(tmp_path, monkeypatch):
    monkeypatch.setattr(ga_export, "PAGE_LIMIT", 1)
    client = PagedClient([["20240101", "1"], ["20240102", "2"]])
    run_export(client, tmp_path, bucket={"name": "x", "dimensions": ["date"], "metrics": ["sessions"]})
    assert len(client.kwargs) == 2
    for kw in client.kwargs:
        assert isinstance(kw["retry"], gretry.Retry)
        assert kw["timeout"] == ga_export.REQUEST_TIMEOUT_SECONDS == 300


def test_export_bucket_uses_header_names_from_response(tmp_path):
    # The API echoes the names back; the CSV header should come from the response, not the YAML.
    client = PagedClient([["20240101", "1"]], dims=["date"], mets=["sessions"])
    _, out = run_export(client, tmp_path, bucket={"name": "x", "dimensions": ["date"], "metrics": ["sessions"]})
    assert read_csv(out)[0] == ["date", "sessions"]


def test_export_bucket_writes_utf8_regardless_of_locale(tmp_path):
    title = "東京 — café ☕"
    client = PagedClient([["20240101", title, "1", "1"]], dims=["date", "pageTitle"], mets=["sessions", "totalUsers"])
    _, out = run_export(client, tmp_path)
    assert title.encode("utf-8") in out.read_bytes()
    assert read_csv(out)[1][1] == title


def test_export_bucket_fails_when_row_count_changes_between_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(ga_export, "PAGE_LIMIT", 1)
    dims, mets = BUCKET["dimensions"], BUCKET["metrics"]
    client = ScriptedClient([
        fake_response(dims, mets, [["20240101", "US", "1", "1"]], 3),
        fake_response(dims, mets, [["20240101", "GB", "1", "1"]], 4),
    ])
    with pytest.raises(ga_export.InconsistentReport, match="row count changed from 3 to 4"):
        run_export(client, tmp_path)
    assert not (tmp_path / "b.csv").exists()


def test_export_bucket_error_propagates_and_leaves_part_file(tmp_path):
    client = ScriptedClient([gexc.InvalidArgument("incompatible")])
    with pytest.raises(gexc.InvalidArgument):
        run_export(client, tmp_path)
    assert not (tmp_path / "b.csv").exists()
    assert (tmp_path / "b.csv.part").exists()  # extract() is responsible for cleaning this up


# ---------------------------------------------------------------- extract


def make_args(tmp_path, config_text, properties="111", start="2024-01-01", end="2024-01-02"):
    return NS(
        properties=properties,
        start_date=start,
        end_date=end,
        config=write_yaml(tmp_path, config_text),
        out=str(tmp_path / "export"),
    )


def bucket_yaml(*specs):
    return "buckets:\n" + "".join(
        f"  - name: {name}\n    dimensions: [{dim}]\n    metrics: [sessions]\n" if dim
        else f"  - name: {name}\n    metrics: [sessions]\n"
        for name, dim in specs
    )


GOOD_CONFIG = bucket_yaml(("good", None))
MIXED_CONFIG = bucket_yaml(("good", None), ("broken", "bad"))


@pytest.fixture
def routing_client(monkeypatch):
    client = RoutingClient()
    monkeypatch.setattr(ga_export, "BetaAnalyticsDataClient", lambda: client)
    return client


def test_extract_writes_one_csv_per_property_per_bucket(tmp_path, routing_client, fixed_today, capsys):
    args = make_args(tmp_path, GOOD_CONFIG, properties="111,properties/222")

    rc = ga_export.extract(args)

    assert rc == 0
    assert (tmp_path / "export" / "111" / "good.csv").exists()
    assert (tmp_path / "export" / "222" / "good.csv").exists()
    assert "Summary: 2 completed, 0 skipped, 0 failed" in capsys.readouterr().out


def test_extract_skips_existing_csv(tmp_path, routing_client, fixed_today, capsys):
    args = make_args(tmp_path, GOOD_CONFIG)
    existing = tmp_path / "export" / "111" / "good.csv"
    existing.parent.mkdir(parents=True)
    existing.write_text("previous content\n")

    rc = ga_export.extract(args)

    assert rc == 0
    assert routing_client.calls == 0
    assert existing.read_text() == "previous content\n"
    assert "Summary: 0 completed, 1 skipped, 0 failed" in capsys.readouterr().out


def test_extract_records_failure_continues_and_cleans_part_file(tmp_path, routing_client, fixed_today, capsys):
    args = make_args(tmp_path, MIXED_CONFIG, properties="111,222")

    rc = ga_export.extract(args)

    out = capsys.readouterr().out
    assert rc == 1
    assert "Summary: 2 completed, 0 skipped, 2 failed" in out
    assert "Failures:" in out
    assert "111  broken  400 Please remove bad to make the request compatible" in out
    assert "222  broken  400 Please remove bad to make the request compatible" in out
    for pid in ("111", "222"):
        assert (tmp_path / "export" / pid / "good.csv").exists()
        assert not (tmp_path / "export" / pid / "broken.csv").exists()
        assert not (tmp_path / "export" / pid / "broken.csv.part").exists()


def test_extract_recovers_from_transient_error(tmp_path, routing_client, fixed_today, fake_clock, capsys):
    args = make_args(tmp_path, bucket_yaml(("flaky", "flaky")))

    rc = ga_export.extract(args)

    assert rc == 0
    assert routing_client.calls == 2
    assert fake_clock == [5]
    assert "Summary: 1 completed, 0 skipped, 0 failed" in capsys.readouterr().out


def test_extract_hourly_quota_still_exhausted_after_an_hour_fails_bucket(tmp_path, routing_client, fixed_today, fake_clock, capsys):
    args = make_args(tmp_path, bucket_yaml(("q", "quota"), ("good", None)))

    rc = ga_export.extract(args)

    out = capsys.readouterr().out
    assert rc == 1
    assert 3600 <= sum(fake_clock) <= ga_export.QUOTA_WAIT_SECONDS
    assert "Summary: 1 completed, 0 skipped, 1 failed" in out
    assert "111  q  Timeout of 3900.0s exceeded, last exception: 429 Exhausted property tokens for a project per hour." in out
    assert not (tmp_path / "export" / "111" / "q.csv.part").exists()
    assert (tmp_path / "export" / "111" / "good.csv").exists()  # the run carried on


def test_extract_daily_quota_stops_the_run(tmp_path, routing_client, fixed_today, fake_clock, capsys, caplog):
    config = bucket_yaml(("a", None), ("b", "daily"), ("c", None))
    args = make_args(tmp_path, config, properties="111,222")

    with caplog.at_level("ERROR", logger="ga_export"):
        rc = ga_export.extract(args)

    out = capsys.readouterr().out
    assert rc == 1
    assert fake_clock == []  # no point waiting: the quota returns at midnight Pacific
    assert routing_client.calls == 2  # 111/a succeeded, 111/b hit the daily quota, nothing after
    assert (tmp_path / "export" / "111" / "a.csv").exists()
    assert not (tmp_path / "export" / "111" / "b.csv.part").exists()
    assert not (tmp_path / "export" / "222").exists() or not any((tmp_path / "export" / "222").glob("*"))
    assert "daily token quota exhausted; it resets at midnight Pacific time. Stopping now" in caplog.text
    assert "Summary: 1 completed, 0 skipped, 5 failed" in out
    assert "  111  b  429 Exhausted property tokens for a property per day." in out
    assert "  111  c  not attempted: daily quota exhausted" in out
    assert "  222  a, b, c  not attempted: daily quota exhausted" in out


def test_extract_inconsistent_report_counts_as_failure(tmp_path, routing_client, fixed_today, monkeypatch, capsys):
    monkeypatch.setattr(ga_export, "PAGE_LIMIT", 1)
    args = make_args(tmp_path, bucket_yaml(("moving", "shifting")))

    rc = ga_export.extract(args)

    out = capsys.readouterr().out
    assert rc == 1
    assert "111  moving  row count changed from 2 to 3 between pages" in out
    assert not (tmp_path / "export" / "111" / "moving.csv").exists()
    assert not (tmp_path / "export" / "111" / "moving.csv.part").exists()


def test_extract_property_403_skips_its_remaining_buckets_and_continues(tmp_path, routing_client, fixed_today, capsys):
    config = bucket_yaml(("a", None), ("b", None), ("c", None))
    args = make_args(tmp_path, config, properties="111,403,222")

    rc = ga_export.extract(args)

    out = capsys.readouterr().out
    assert rc == 1
    # 111 and 222 export all three buckets; 403 is attempted exactly once.
    assert [r.property for r in routing_client.requests].count("properties/403") == 1
    assert routing_client.calls == 7
    for pid in ("111", "222"):
        for name in ("a", "b", "c"):
            assert (tmp_path / "export" / pid / f"{name}.csv").exists()
    assert not any((tmp_path / "export" / "403").glob("*"))
    assert "Summary: 6 completed, 0 skipped, 3 failed" in out
    assert "  403  a  403 User does not have sufficient permissions for this property. [PERMISSION_DENIED]" in out
    assert "  403  b, c  not attempted: no access to property 403" in out
    # The multi-line gRPC details block must not leak into the log or summary.
    assert 'domain: "' not in out
    assert "reason: " not in out


def test_extract_property_403_on_last_bucket_records_only_that_bucket(tmp_path, routing_client, fixed_today, capsys):
    args = make_args(tmp_path, GOOD_CONFIG, properties="403")
    assert ga_export.extract(args) == 1
    out = capsys.readouterr().out
    assert "Summary: 0 completed, 0 skipped, 1 failed" in out
    assert "not attempted" not in out


def test_extract_failure_lines_are_single_line(tmp_path, routing_client, fixed_today, capsys):
    args = make_args(tmp_path, MIXED_CONFIG)
    ga_export.extract(args)
    out = capsys.readouterr().out
    failure_lines = out.split("Failures:\n", 1)[1].splitlines()
    assert failure_lines == ["  111  broken  400 Please remove bad to make the request compatible"]


def test_extract_rerun_after_failure_retries_only_failed_bucket(tmp_path, routing_client, fixed_today, capsys):
    args = make_args(tmp_path, MIXED_CONFIG)

    ga_export.extract(args)
    calls_after_first = routing_client.calls
    ga_export.extract(args)

    assert routing_client.calls == calls_after_first + 1  # only "broken" was attempted again
    assert "Summary: 0 completed, 1 skipped, 1 failed" in capsys.readouterr().out


def test_extract_duplicate_property_is_exported_once(tmp_path, routing_client, fixed_today, capsys):
    args = make_args(tmp_path, GOOD_CONFIG, properties="111,111")
    assert ga_export.extract(args) == 0
    assert routing_client.calls == 1
    assert "Summary: 1 completed, 0 skipped, 0 failed" in capsys.readouterr().out


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"start": "2024-02-01", "end": "2024-01-01"}, "is after --end-date"),
        ({"start": "bad"}, "--start-date must be YYYY-MM-DD"),
        ({"end": "bad"}, "--end-date must be YYYY-MM-DD"),
        ({"end": "2026-10-01"}, "--end-date 2026-10-01 is in the future"),
        ({"properties": " , "}, "at least one property ID"),
        ({"properties": "properties/"}, "is not a GA4 property ID"),
    ],
)
def test_extract_rejects_bad_arguments_before_any_api_call(tmp_path, routing_client, fixed_today, kwargs, message):
    with pytest.raises(SystemExit) as exc:
        ga_export.extract(make_args(tmp_path, GOOD_CONFIG, **kwargs))
    assert message in str(exc.value)
    assert routing_client.calls == 0


@pytest.mark.parametrize("days_ago, warns", [(0, True), (1, True), (2, True), (3, False), (30, False)])
def test_extract_warns_when_end_date_is_recent(tmp_path, routing_client, fixed_today, caplog, days_ago, warns):
    end = (fixed_today - timedelta(days=days_ago)).isoformat()
    args = make_args(tmp_path, GOOD_CONFIG, start="2024-01-01", end=end)
    with caplog.at_level("WARNING", logger="ga_export"):
        assert ga_export.extract(args) == 0
    assert ("GA4 may still be processing" in caplog.text) is warns


def test_extract_uses_real_today_by_default():
    assert ga_export.today() == date.today()


# ---------------------------------------------------------------- list_properties


def admin_client(monkeypatch, summaries=None, error=None):
    def list_account_summaries():
        if error:
            raise error
        return summaries
    monkeypatch.setattr(ga_export, "AnalyticsAdminServiceClient",
                        lambda: NS(list_account_summaries=list_account_summaries))


def test_list_properties_prints_table(monkeypatch, capsys):
    admin_client(monkeypatch, summaries=[
        NS(display_name="Acme", property_summaries=[
            NS(property="properties/1", display_name="Acme Web"),
            NS(property="properties/22", display_name="Acme App"),
        ]),
        NS(display_name="Beta Co", property_summaries=[]),
        NS(display_name="Gamma", property_summaries=[NS(property="properties/333", display_name="G")]),
    ])

    assert ga_export.list_properties(NS()) == 0

    lines = capsys.readouterr().out.splitlines()
    # Column widths come from the widest value in each column, header included.
    assert lines == [
        f"{'ACCOUNT':<7}  {'PROPERTY_ID':<11}  PROPERTY_NAME",
        f"{'Acme':<7}  {'1':<11}  Acme Web",
        f"{'Acme':<7}  {'22':<11}  Acme App",
        f"{'Gamma':<7}  {'333':<11}  G",
    ]


def test_list_properties_handles_no_properties(monkeypatch, capsys):
    admin_client(monkeypatch, summaries=[])
    assert ga_export.list_properties(NS()) == 0
    assert "No accessible properties found." in capsys.readouterr().out


def test_list_properties_reports_api_errors_cleanly(monkeypatch):
    admin_client(monkeypatch, error=gexc.PermissionDenied("Google Analytics Admin API has not been used"))
    with pytest.raises(SystemExit) as exc:
        ga_export.list_properties(NS())
    assert str(exc.value) == "Could not list properties: 403 Google Analytics Admin API has not been used"


# ---------------------------------------------------------------- main / CLI wiring


def test_main_requires_subcommand(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ga_export.py"])
    with pytest.raises(SystemExit) as exc:
        ga_export.main()
    assert exc.value.code == 2


def test_main_extract_requires_dates_and_properties(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["ga_export.py", "extract", "--properties", "1"])
    with pytest.raises(SystemExit) as exc:
        ga_export.main()
    assert exc.value.code == 2


def test_main_extract_defaults_and_dispatch(monkeypatch):
    seen = {}
    monkeypatch.setattr(ga_export, "extract", lambda args: seen.update(vars(args)) or 7)
    monkeypatch.setattr(sys, "argv", [
        "ga_export.py", "extract", "--properties", "1,2", "--start-date", "2024-01-01", "--end-date", "2024-01-31",
    ])
    assert ga_export.main() == 7
    assert seen["properties"] == "1,2"
    assert seen["config"] == "buckets.yaml"
    assert seen["out"] == "./export"


@pytest.mark.parametrize("error", [DefaultCredentialsError("no creds"), RefreshError("invalid_grant: key revoked")])
def test_main_reports_credential_problems_cleanly(monkeypatch, error):
    def boom(args):
        raise error
    monkeypatch.setattr(ga_export, "list_properties", boom)
    monkeypatch.setattr(sys, "argv", ["ga_export.py", "list-properties"])
    with pytest.raises(SystemExit) as exc:
        ga_export.main()
    assert "GOOGLE_APPLICATION_CREDENTIALS" in str(exc.value)
    assert str(error) in str(exc.value)
