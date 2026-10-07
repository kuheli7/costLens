from datetime import date, timedelta

from costlens.baseline import (
    AnomalyConfig,
    check_day,
    compute_baseline,
    daily_series,
    find_anomalies,
)
from costlens.models import CostRow
from costlens.synth import INCIDENT_KINDS, generate

END = date(2026, 10, 6)  # a Tuesday


def weekly(base_weekday, base_weekend, days=60, end=END, spikes=None):
    spikes = spikes or {}
    out = {}
    for i in range(days):
        d = end - timedelta(days=days - 1 - i)
        out[d] = spikes.get(d, base_weekday if d.weekday() < 5 else base_weekend)
    return out


def test_weekend_dip_is_not_an_anomaly_but_a_weekday_level_on_a_sunday_is():
    series = weekly(10_000, 5_000)
    sunday = date(2026, 10, 4)
    assert series[sunday] == 5_000 and check_day("total", series, sunday) is None
    series[sunday] = 10_000  # weekday-level spend on a Sunday
    a = check_day("total", series, sunday)
    assert a and a.delta_cents == 5_000 and a.method == "same_weekday"


def test_one_past_spike_does_not_distort_the_baseline():
    last_tuesday = END - timedelta(days=7)
    clean = compute_baseline(weekly(10_000, 5_000), END)
    spiked = compute_baseline(weekly(10_000, 5_000, spikes={last_tuesday: 90_000}), END)
    assert clean.expected_cents == spiked.expected_cents == 10_000


def test_needs_both_relative_and_absolute_size():
    # +60% but only 30 cents: tiny service, must not alert
    tiny = weekly(50, 50)
    tiny[END] = 80
    assert check_day("tiny", tiny, END) is None
    # +$50 but only +0.5% on a big bill: must not alert
    big = weekly(1_000_000, 1_000_000)
    big[END] = 1_005_000
    assert check_day("big", big, END) is None


def test_alert_when_large_in_both_terms():
    series = weekly(10_000, 5_000)
    series[END] = 14_000  # +40%, +$40
    a = check_day("total", series, END)
    assert a and a.delta_cents == 4_000 and round(a.pct, 2) == 0.40 and a.z >= 3


def test_new_subject_with_no_history_is_flagged_as_new():
    a = check_day("AmazonES", {END: 6_720}, END)
    assert a and a.method == "new" and a.pct is None and a.expected_cents == 0


def test_too_little_history_falls_back_to_trailing_week_then_to_nothing():
    short = {END - timedelta(days=k): 1_000 for k in range(1, 6)}  # 5 days, <3 same weekday
    short[END] = 5_000
    base = compute_baseline(short, END)
    assert base.method == "trailing" and base.expected_cents == 1_000
    assert compute_baseline({END - timedelta(days=1): 1_000, END: 5_000}, END) is None


def test_days_before_a_service_existed_are_not_counted_as_zero():
    series = {END - timedelta(days=k): 1_000 for k in range(1, 11)}  # exists for 10 days
    series[END] = 5_000
    base = compute_baseline(series, END)
    assert base.expected_cents == 1_000 and base.method == "trailing" and base.n_points == 7


def _rows(day, cents, line="Usage", service="EC2"):
    return CostRow(day, service, "r", "U", "x", "t", "", 1.0, cents, line)


def test_tax_credits_refunds_and_monthly_fees_are_excluded_from_the_series():
    d = date(2026, 10, 1)
    rows = [
        _rows(d, 1_000),
        _rows(d, 180, "Tax"),
        _rows(d, -500, "Credit"),
        _rows(d, -50, "Refund"),
        _rows(d, 3_000, "Fee"),
    ]
    assert daily_series(rows) == {d: 1_000}
    assert daily_series(rows, exclude_fees=False) == {d: 4_000}


def test_no_false_alarm_on_ordinary_days_including_month_start_fee():
    ds = generate(seed=3)  # 90 days, no incident, includes the 1st with a support fee
    alarms = []
    for k in range(0, 45):
        day = ds.last_day - timedelta(days=k)
        alarms += find_anomalies([r for r in ds.rows if r.day <= day], day)
    assert alarms == []


def test_every_incident_kind_is_detected_on_the_total_or_its_service():
    missed = []
    for kind in INCIDENT_KINDS:
        ds = generate(seed=11, incident=kind)
        if not find_anomalies(ds.rows, ds.last_day):
            missed.append(kind)
    # the RDS rate change is +58% of its own service; it must be caught at service level
    assert missed == []


def test_config_thresholds_are_respected():
    series = weekly(10_000, 5_000)
    series[END] = 12_000  # +20%
    assert check_day("total", series, END) is None
    assert check_day("total", series, END, AnomalyConfig(min_pct=0.1, min_abs_cents=100))


def test_service_first_seen_two_days_ago_is_still_reported_as_new():
    series = {END - timedelta(days=2): 700, END - timedelta(days=1): 700, END: 700}
    assert check_day("AmazonES", series, END) is None  # total-style check: too little history
    a = check_day("AmazonES", series, END, short_history_is_new=True)
    assert a and a.method == "new" and a.expected_cents == 0 and a.delta_cents == 700


def test_a_service_with_a_week_of_history_is_judged_normally_not_as_new():
    series = {END - timedelta(days=k): 1_000 for k in range(1, 8)}
    series[END] = 1_020
    assert check_day("x", series, END, short_history_is_new=True) is None


def test_persisting_new_service_is_found_days_after_it_appeared():
    ds = generate(seed=0, incident="new_service", incident_age=2)
    anomalies = find_anomalies(ds.rows, ds.last_day)
    assert any(a.subject == "AmazonES" for a in anomalies)
