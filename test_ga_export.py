"""Unit tests for ga_export.py. No network: the GA4 clients are replaced with fakes."""

import csv
import sys
from types import SimpleNamespace as NS

import pytest
from google.api_core import exceptions as gexc
from google.auth.exceptions import DefaultCredentialsError

import ga_export


# ---------------------------------------------------------------- helpers


def fake_response(dims, mets, rows, row_count):
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
    )


class PagedClient:
    """Fake Data API client that serves `all_rows` in pages of `request.limit`."""

    def __init__(self, all_rows, dims=("date",), mets=("sessions",)):
        self.all_rows = all_rows
        self.dims, self.mets = list(dims), list(mets)
        self.requests = []

    def run_report(self, request):
        self.requests.append(request)
        page = self.all_rows[request.offset : request.offset + request.limit]
        return fake_response(self.dims, self.mets, page, len(self.all_rows))


class ScriptedClient:
    """Fake Data API client that returns or raises each scripted item in turn."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def run_report(self, request):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def write_yaml(tmp_path, text):
    p = tmp_path / "buckets.yaml"
    p.write_text(text)
    return str(p)


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


@pytest.fixture
def no_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(ga_export.time, "sleep", sleeps.append)
    return sleeps


# ---------------------------------------------------------------- load_buckets


def test_load_buckets_shipped_config_is_valid():
    buckets = ga_export.load_buckets("buckets.yaml")
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
        ("buckets:\n  - name: a\n    dimensions: country\n    metrics: [x]\n", "'dimensions' must be a list of strings"),
        ("buckets:\n  - name: a\n    dimensions: [1]\n    metrics: [x]\n", "'dimensions' must be a list of strings"),
        ("buckets:\n  - name: a\n    dimensions: []\n", "'metrics' must be a non-empty list of strings"),
        ("buckets:\n  - name: a\n    metrics: []\n", "'metrics' must be a non-empty list of strings"),
        ("buckets:\n  - name: a\n    metrics: sessions\n", "'metrics' must be a non-empty list of strings"),
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


# ---------------------------------------------------------------- parse_date


def test_parse_date_valid():
    assert ga_export.parse_date("2024-02-29", "--start-date").isoformat() == "2024-02-29"


@pytest.mark.parametrize("value", ["2024-13-01", "2023-02-29", "01/02/2024", "20240101", ""])
def test_parse_date_invalid(value):
    with pytest.raises(SystemExit, match="--end-date must be YYYY-MM-DD"):
        ga_export.parse_date(value, "--end-date")


# ---------------------------------------------------------------- run_with_retry


def test_retry_returns_immediately_on_success(no_sleep):
    client = ScriptedClient(["ok"])
    assert ga_export.run_with_retry(client, "req", "[l]") == "ok"
    assert client.calls == 1
    assert no_sleep == []


def test_retry_backs_off_exponentially_on_quota_errors(no_sleep):
    client = ScriptedClient([gexc.ResourceExhausted("tokens"), gexc.TooManyRequests("slow down"), "ok"])
    assert ga_export.run_with_retry(client, "req", "[l]") == "ok"
    assert client.calls == 3
    assert no_sleep == [5, 10]


def test_retry_caps_backoff_and_gives_up_after_max_retries(no_sleep):
    client = ScriptedClient([gexc.ResourceExhausted("tokens")] * (ga_export.MAX_RETRIES + 1))
    with pytest.raises(gexc.ResourceExhausted):
        ga_export.run_with_retry(client, "req", "[l]")
    assert client.calls == ga_export.MAX_RETRIES + 1
    assert len(no_sleep) == ga_export.MAX_RETRIES
    assert no_sleep == [5, 10, 20, 40, 80, 160, 300, 300]
    assert max(no_sleep) <= ga_export.MAX_BACKOFF_SECONDS


def test_retry_does_not_retry_other_api_errors(no_sleep):
    client = ScriptedClient([gexc.InvalidArgument("bad dimension"), "never reached"])
    with pytest.raises(gexc.InvalidArgument):
        ga_export.run_with_retry(client, "req", "[l]")
    assert client.calls == 1
    assert no_sleep == []


# ---------------------------------------------------------------- export_bucket


BUCKET = {"name": "b", "dimensions": ["date", "country"], "metrics": ["sessions", "totalUsers"]}


def test_export_bucket_paginates_and_writes_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(ga_export, "PAGE_LIMIT", 3)
    rows = [[f"2024010{i}", "US", str(i), str(i * 10)] for i in range(1, 8)]
    client = PagedClient(rows, dims=BUCKET["dimensions"], mets=BUCKET["metrics"])
    out = tmp_path / "b.csv"

    n = ga_export.export_bucket(client, "123", BUCKET, "2024-01-01", "2024-01-07", str(out), "[l]")

    assert n == 7
    assert read_csv(out) == [["date", "country", "sessions", "totalUsers"]] + rows
    assert not (tmp_path / "b.csv.part").exists()
    assert [r.offset for r in client.requests] == [0, 3, 6]
    assert all(r.limit == 3 for r in client.requests)


def test_export_bucket_stops_when_row_count_is_exact_multiple_of_page(tmp_path, monkeypatch):
    monkeypatch.setattr(ga_export, "PAGE_LIMIT", 3)
    rows = [[f"2024010{i}", "US", "1", "1"] for i in range(1, 7)]
    client = PagedClient(rows, dims=BUCKET["dimensions"], mets=BUCKET["metrics"])
    n = ga_export.export_bucket(client, "123", BUCKET, "2024-01-01", "2024-01-06", str(tmp_path / "b.csv"), "[l]")
    assert n == 6
    assert [r.offset for r in client.requests] == [0, 3]


def test_export_bucket_empty_result_writes_header_only(tmp_path):
    client = PagedClient([], dims=BUCKET["dimensions"], mets=BUCKET["metrics"])
    out = tmp_path / "b.csv"
    n = ga_export.export_bucket(client, "123", BUCKET, "2024-01-01", "2024-01-07", str(out), "[l]")
    assert n == 0
    assert read_csv(out) == [["date", "country", "sessions", "totalUsers"]]


def test_export_bucket_builds_request_from_bucket_and_dates(tmp_path):
    client = PagedClient([], dims=BUCKET["dimensions"], mets=BUCKET["metrics"])
    ga_export.export_bucket(client, "123", BUCKET, "2024-01-01", "2024-01-07", str(tmp_path / "b.csv"), "[l]")
    [req] = client.requests
    assert req.property == "properties/123"
    assert [d.name for d in req.dimensions] == ["date", "country"]
    assert [m.name for m in req.metrics] == ["sessions", "totalUsers"]
    assert req.date_ranges[0].start_date == "2024-01-01"
    assert req.date_ranges[0].end_date == "2024-01-07"
    assert req.order_bys[0].dimension.dimension_name == "date"
    assert req.limit == ga_export.PAGE_LIMIT == 100000
    assert req.offset == 0


def test_export_bucket_uses_header_names_from_response(tmp_path):
    # The API echoes the names back; the CSV header should come from the response, not the YAML.
    client = PagedClient([["20240101", "1"]], dims=["date"], mets=["sessions"])
    out = tmp_path / "b.csv"
    ga_export.export_bucket(client, "1", {"name": "x", "dimensions": ["date"], "metrics": ["sessions"]},
                            "2024-01-01", "2024-01-01", str(out), "[l]")
    assert read_csv(out)[0] == ["date", "sessions"]


def test_export_bucket_error_propagates_and_leaves_part_file(tmp_path):
    client = ScriptedClient([gexc.InvalidArgument("incompatible")])
    out = tmp_path / "b.csv"
    with pytest.raises(gexc.InvalidArgument):
        ga_export.export_bucket(client, "1", BUCKET, "2024-01-01", "2024-01-01", str(out), "[l]")
    assert not out.exists()
    assert (tmp_path / "b.csv.part").exists()  # extract() is responsible for cleaning this up


# ---------------------------------------------------------------- extract


class RoutingClient:
    """Fake Data API client: behaviour depends on which non-date dimension the bucket asks for."""

    def __init__(self):
        self.calls = 0

    def run_report(self, request):
        self.calls += 1
        dims = [d.name for d in request.dimensions]
        mets = [m.name for m in request.metrics]
        if "bad" in dims:
            raise gexc.InvalidArgument("Please remove bad to make the request compatible")
        if "quota" in dims:
            raise gexc.ResourceExhausted("Exhausted property tokens")
        return fake_response(dims, mets, [["20240101"] + ["x"] * (len(dims) - 1) + ["1"] * len(mets)], 1)


def make_args(tmp_path, config_text, properties="111", start="2024-01-01", end="2024-01-02"):
    return NS(
        properties=properties,
        start_date=start,
        end_date=end,
        config=write_yaml(tmp_path, config_text),
        out=str(tmp_path / "export"),
    )


GOOD_CONFIG = "buckets:\n  - name: good\n    metrics: [sessions]\n"
MIXED_CONFIG = (
    "buckets:\n"
    "  - name: good\n    metrics: [sessions]\n"
    "  - name: broken\n    dimensions: [bad]\n    metrics: [sessions]\n"
)


def test_extract_writes_one_csv_per_property_per_bucket(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ga_export, "BetaAnalyticsDataClient", RoutingClient)
    args = make_args(tmp_path, GOOD_CONFIG, properties="111,properties/222")

    rc = ga_export.extract(args)

    assert rc == 0
    assert (tmp_path / "export" / "111" / "good.csv").exists()
    assert (tmp_path / "export" / "222" / "good.csv").exists()
    assert "Summary: 2 completed, 0 skipped, 0 failed" in capsys.readouterr().out


def test_extract_skips_existing_csv(tmp_path, monkeypatch, capsys):
    client = RoutingClient()
    monkeypatch.setattr(ga_export, "BetaAnalyticsDataClient", lambda: client)
    args = make_args(tmp_path, GOOD_CONFIG)
    existing = tmp_path / "export" / "111" / "good.csv"
    existing.parent.mkdir(parents=True)
    existing.write_text("previous content\n")

    rc = ga_export.extract(args)

    assert rc == 0
    assert client.calls == 0
    assert existing.read_text() == "previous content\n"
    assert "Summary: 0 completed, 1 skipped, 0 failed" in capsys.readouterr().out


def test_extract_records_failure_continues_and_cleans_part_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ga_export, "BetaAnalyticsDataClient", RoutingClient)
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


def test_extract_exhausted_retries_count_as_failure(tmp_path, monkeypatch, no_sleep, capsys):
    monkeypatch.setattr(ga_export, "BetaAnalyticsDataClient", RoutingClient)
    args = make_args(tmp_path, "buckets:\n  - name: q\n    dimensions: [quota]\n    metrics: [sessions]\n")

    rc = ga_export.extract(args)

    assert rc == 1
    assert len(no_sleep) == ga_export.MAX_RETRIES
    assert "Summary: 0 completed, 0 skipped, 1 failed" in capsys.readouterr().out


def test_extract_rerun_after_failure_retries_only_failed_bucket(tmp_path, monkeypatch, capsys):
    client = RoutingClient()
    monkeypatch.setattr(ga_export, "BetaAnalyticsDataClient", lambda: client)
    args = make_args(tmp_path, MIXED_CONFIG)

    ga_export.extract(args)
    calls_after_first = client.calls
    ga_export.extract(args)

    assert client.calls == calls_after_first + 1  # only "broken" was attempted again
    assert "Summary: 0 completed, 1 skipped, 1 failed" in capsys.readouterr().out


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"start": "2024-02-01", "end": "2024-01-01"}, "is after --end-date"),
        ({"start": "bad"}, "--start-date must be YYYY-MM-DD"),
        ({"end": "bad"}, "--end-date must be YYYY-MM-DD"),
        ({"properties": " , "}, "at least one property ID"),
    ],
)
def test_extract_rejects_bad_arguments_before_any_api_call(tmp_path, monkeypatch, kwargs, message):
    client = RoutingClient()
    monkeypatch.setattr(ga_export, "BetaAnalyticsDataClient", lambda: client)
    with pytest.raises(SystemExit) as exc:
        ga_export.extract(make_args(tmp_path, GOOD_CONFIG, **kwargs))
    assert message in str(exc.value)
    assert client.calls == 0


# ---------------------------------------------------------------- list_properties


def test_list_properties_prints_table(monkeypatch, capsys):
    summaries = [
        NS(display_name="Acme", property_summaries=[
            NS(property="properties/1", display_name="Acme Web"),
            NS(property="properties/22", display_name="Acme App"),
        ]),
        NS(display_name="Beta Co", property_summaries=[]),
        NS(display_name="Gamma", property_summaries=[NS(property="properties/333", display_name="G")]),
    ]
    monkeypatch.setattr(ga_export, "AnalyticsAdminServiceClient",
                        lambda: NS(list_account_summaries=lambda: summaries))

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
    monkeypatch.setattr(ga_export, "AnalyticsAdminServiceClient",
                        lambda: NS(list_account_summaries=lambda: []))
    assert ga_export.list_properties(NS()) == 0
    assert "No accessible properties found." in capsys.readouterr().out


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


def test_main_reports_missing_credentials_cleanly(monkeypatch):
    def boom(args):
        raise DefaultCredentialsError("no creds")
    monkeypatch.setattr(ga_export, "list_properties", boom)
    monkeypatch.setattr(sys, "argv", ["ga_export.py", "list-properties"])
    with pytest.raises(SystemExit) as exc:
        ga_export.main()
    assert "GOOGLE_APPLICATION_CREDENTIALS" in str(exc.value)
