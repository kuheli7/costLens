from datetime import date, timedelta

import boto3
import duckdb
import pytest
from moto import mock_aws

from costlens.ce import fetch_daily_totals
from costlens.curmap import normalise_line_type, read_cur_parquet, rows_from_cur, tag_lookup
from costlens.models import CostRow
from costlens.store import DuckStore, S3ParquetStore, history_window, read_parquet, write_parquet
from costlens.synth import generate

D1, D2 = date(2026, 10, 5), date(2026, 10, 6)


def row(day, rid="i-1", cents=100, line="Usage"):
    return CostRow(day, "AmazonEC2", "ap-south-1", "BoxUsage", rid, "t", "g", 24.0, cents, line)


# ----------------------------------------------------------------- stores


@pytest.fixture(params=["duck", "s3"])
def store(request):
    if request.param == "duck":
        yield DuckStore()
    else:
        with mock_aws():
            s3 = boto3.client("s3", region_name="ap-south-1")
            s3.create_bucket(
                Bucket="costlens-test",
                CreateBucketConfiguration={"LocationConstraint": "ap-south-1"},
            )
            yield S3ParquetStore(s3, "costlens-test")


def test_load_and_read_back_round_trip(store):
    rows = [row(D1, "i-1", 100), row(D1, "i-2", 250, "Tax")]
    store.load_day(D1, rows)
    assert store.read_range(D1, D1) == sorted(rows, key=lambda r: (r.resource_id, r.line_type))
    assert store.days() == [D1]


def test_rerunning_a_day_replaces_it_and_never_duplicates(store):
    store.load_day(D1, [row(D1, "i-1", 100), row(D1, "i-2", 200)])
    store.load_day(D1, [row(D1, "i-1", 100), row(D1, "i-2", 200)])  # same data again
    assert len(store.read_range(D1, D1)) == 2
    store.load_day(D1, [row(D1, "i-1", 999)])  # revised data replaces the whole day
    assert [(r.resource_id, r.cost_cents) for r in store.read_range(D1, D1)] == [("i-1", 999)]


def test_loading_one_day_leaves_other_days_alone(store):
    store.load_day(D1, [row(D1, cents=1)])
    store.load_day(D2, [row(D2, cents=2)])
    store.load_day(D1, [row(D1, cents=3)])
    assert [(r.day, r.cost_cents) for r in store.read_range(D1, D2)] == [(D1, 3), (D2, 2)]
    assert store.days() == [D1, D2]


def test_rows_for_the_wrong_day_are_refused(store):
    with pytest.raises(ValueError, match="not for"):
        store.load_day(D1, [row(D2)])


def test_empty_day_is_recorded_as_loaded(store):
    store.load_day(D1, [])
    assert store.days() == [D1] and store.read_range(D1, D1) == []


def test_range_read_is_inclusive_and_bounded(store):
    for k in range(5):
        d = D1 + timedelta(days=k)
        store.load_day(d, [row(d, cents=k)])
    got = store.read_range(D1 + timedelta(days=1), D1 + timedelta(days=3))
    assert [r.cost_cents for r in got] == [1, 2, 3]


def test_synthetic_dataset_survives_a_parquet_round_trip(tmp_path):
    ds = generate(seed=3, days=10, incident="new_service")
    path = str(tmp_path / "x.parquet")
    write_parquet(ds.rows, path)
    back = read_parquet([path])
    assert sorted(back, key=lambda r: (r.day, r.key, r.line_type)) == sorted(
        ds.rows, key=lambda r: (r.day, r.key, r.line_type)
    )


def test_history_window_covers_four_same_weekdays():
    start, end = history_window(D2)
    assert end == D2 and (D2 - start).days >= 28


# ----------------------------------------------------------------- CUR mapping


def cur_line(
    hour, cost, usage=1, rid="i-0abc", line="Usage", tags=None, day=D2, ut="BoxUsage:m5.large"
):
    return {
        "line_item_usage_start_date": f"{day.isoformat()}T{hour:02d}:00:00Z",
        "line_item_unblended_cost": cost,
        "line_item_usage_amount": usage,
        "line_item_resource_id": rid,
        "line_item_product_code": "AmazonEC2",
        "product_region_code": "ap-south-1",
        "line_item_usage_type": ut,
        "line_item_line_item_type": line,
        "tags": tags,
    }


def test_hourly_lines_aggregate_to_one_daily_row_without_rounding_drift():
    lines = [cur_line(h, "0.0125") for h in range(24)]  # 24 x 1.25 cents = 30 cents
    (r,) = rows_from_cur(lines)
    assert (r.day, r.cost_cents, r.usage_qty, r.resource_id) == (D2, 30, 24.0, "i-0abc")


def test_each_line_type_stays_separate_and_is_normalised():
    lines = [
        cur_line(0, "1.00"),
        cur_line(0, "0.18", line="Tax", rid=""),
        cur_line(0, "-0.50", line="Credit", rid=""),
        cur_line(0, "30.00", line="SavingsPlanRecurringFee", rid=""),
    ]
    got = {r.line_type: r.cost_cents for r in rows_from_cur(lines)}
    assert got == {"Usage": 100, "Tax": 18, "Credit": -50, "Fee": 3000}


@pytest.mark.parametrize(
    "raw, expect",
    [
        ("DiscountedUsage", "Usage"),
        ("RIFee", "Fee"),
        ("EdpDiscount", "Credit"),
        (None, "Usage"),
        ("SomethingNew", "Usage"),
    ],
)
def test_line_type_mapping(raw, expect):
    assert normalise_line_type(raw) == expect


@pytest.mark.parametrize(
    "raw",
    [
        {"user_team": "backend"},
        {"key": ["user_team", "x"], "value": ["backend", "y"]},
        [{"key": "user_team", "value": "backend"}],
        '{"user_team": "backend"}',
    ],
)
def test_tags_are_read_from_every_shape_they_may_arrive_in(raw):
    assert tag_lookup(raw, ("user_team",)) == "backend"


def test_missing_or_broken_tags_read_as_untagged():
    for raw in (None, {}, "not json", 42, {"user_team": ""}):
        assert tag_lookup(raw, ("user_team",)) == ""


def test_team_and_asg_tags_are_extracted():
    tags = {"user_team": "backend", "aws_autoscaling_groupName": "api-prod-asg"}
    (r,) = rows_from_cur([cur_line(0, "1.00", tags=tags)])
    assert (r.team, r.asg) == ("backend", "api-prod-asg")


def test_cur_parquet_with_a_map_tags_column_is_read_end_to_end(tmp_path):
    path = str(tmp_path / "cur.parquet")
    con = duckdb.connect()
    con.execute(
        """CREATE TABLE cur (
            line_item_usage_start_date TIMESTAMP, line_item_unblended_cost DECIMAL(18,8),
            line_item_usage_amount DOUBLE, line_item_resource_id VARCHAR,
            line_item_product_code VARCHAR, product_region_code VARCHAR,
            line_item_usage_type VARCHAR, line_item_line_item_type VARCHAR,
            tags MAP(VARCHAR, VARCHAR))"""
    )
    for h in range(24):
        con.execute(
            "INSERT INTO cur VALUES (?, ?, 1, 'i-0abc', 'AmazonEC2', 'ap-south-1', "
            "'BoxUsage:m5.large', 'Usage', MAP {'user_team': 'backend'})",
            [f"2026-10-06 {h:02d}:00:00", "0.10"],
        )
    con.execute(f"COPY cur TO '{path}' (FORMAT PARQUET)")
    (r,) = read_cur_parquet([path])
    assert (r.cost_cents, r.usage_qty, r.team, r.service) == (240, 24.0, "backend", "AmazonEC2")


def test_cur_file_without_the_expected_columns_fails_loudly(tmp_path):
    path = str(tmp_path / "bad.parquet")
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT 1 AS something_else")
    con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")
    with pytest.raises(ValueError, match="missing expected columns"):
        read_cur_parquet([path])


# ----------------------------------------------------------------- Cost Explorer


class StubCE:
    def __init__(self):
        self.calls = []

    def get_cost_and_usage(self, **kw):
        self.calls.append(kw)
        if "NextPageToken" not in kw:
            return {
                "ResultsByTime": [
                    {
                        "TimePeriod": {"Start": "2026-10-05", "End": "2026-10-06"},
                        "Total": {"UnblendedCost": {"Amount": "142.305", "Unit": "USD"}},
                    }
                ],
                "NextPageToken": "t2",
            }
        return {
            "ResultsByTime": [
                {
                    "TimePeriod": {"Start": "2026-10-06", "End": "2026-10-07"},
                    "Total": {"UnblendedCost": {"Amount": "0.01", "Unit": "USD"}},
                }
            ]
        }


def test_cost_explorer_totals_are_parsed_paged_and_rounded_to_cents():
    ce = StubCE()
    totals = fetch_daily_totals(ce, D1, D2)
    assert totals == {D1: 14231, D2: 1}
    assert len(ce.calls) == 2
    assert ce.calls[0]["TimePeriod"] == {"Start": "2026-10-05", "End": "2026-10-07"}
    assert ce.calls[0]["Granularity"] == "DAILY" and ce.calls[0]["Metrics"] == ["UnblendedCost"]
