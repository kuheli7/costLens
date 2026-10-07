from datetime import UTC, date, datetime

from costlens.dataquality import non_usage_cents, provisional_warning, reconcile, untagged_spend
from costlens.models import CostRow

D = date(2026, 10, 6)


def r(cents, team="x", rid="i-1", line="Usage"):
    return CostRow(D, "EC2", "r", "U", rid, team, "", 1.0, cents, line)


def test_recent_day_is_provisional_until_72_hours_after_it_ends():
    assert provisional_warning(D, datetime(2026, 10, 8, 9, tzinfo=UTC))  # 33h after end
    assert provisional_warning(D, datetime(2026, 10, 10, 0, tzinfo=UTC)) is None  # 72h after end


def test_reconcile_passes_within_tolerance_and_fails_beyond():
    assert reconcile(10_000, 10_050).ok
    assert reconcile(10_000, 10_150).ok  # 1.5% gap
    bad = reconcile(10_000, 12_000)
    assert not bad.ok and "incomplete" in bad.message


def test_reconcile_tolerates_cent_level_noise_on_tiny_days():
    assert reconcile(20, 90).ok  # under the absolute floor


def test_untagged_spend_names_the_resources_and_ignores_tax():
    rows = [r(9_200), r(800, team="", rid="i-orphan"), r(180, team="", rid="", line="Tax")]
    u = untagged_spend(rows)
    assert u.cents == 800 and round(u.share, 2) == 0.08 and u.resources == ("i-orphan",)


def test_non_usage_totals_are_reported_by_type():
    rows = [r(100), r(18, line="Tax"), r(-30, line="Credit"), r(5, line="Tax")]
    assert non_usage_cents(rows) == {"Tax": 23, "Credit": -30}
