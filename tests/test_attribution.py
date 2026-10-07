from datetime import date
from fractions import Fraction

from hypothesis import given, settings
from hypothesis import strategies as st

from costlens.attribution import (
    Cell,
    baseline_table,
    contributions,
    drill_down,
    table_from_rows,
    total_change,
    usage_rows,
    volume_rate_split,
)
from costlens.models import DIMENSIONS, CostRow
from costlens.money import round_half_up

D = date(2026, 10, 6)


def row(
    service="EC2",
    region="r1",
    usage="U",
    team="t",
    asg="",
    rid="x",
    qty=1.0,
    cents=100,
    day=D,
    line="Usage",
):
    return CostRow(day, service, region, usage, rid, team, asg, qty, cents, line)


# ----------------------------------------------------------------- volume / rate


def test_pure_volume_change():
    s = volume_rate_split(Cell(Fraction(1000), Fraction(10)), Cell(Fraction(2600), Fraction(26)))
    assert (s.volume_cents, s.rate_cents) == (1600, 0)


def test_pure_rate_change():
    s = volume_rate_split(Cell(Fraction(1000), Fraction(10)), Cell(Fraction(2000), Fraction(10)))
    assert (s.volume_cents, s.rate_cents) == (0, 1000)


def test_mixed_change_matches_the_formula():
    # q 10 -> 20, price 100 -> 150: volume (20-10)*100 = 1000, rate (150-100)*20 = 1000
    s = volume_rate_split(Cell(Fraction(1000), Fraction(10)), Cell(Fraction(3000), Fraction(20)))
    assert (s.volume_cents, s.rate_cents) == (1000, 1000)


def test_new_line_item_is_all_volume():
    s = volume_rate_split(Cell(Fraction(0), Fraction(0)), Cell(Fraction(500), Fraction(5)))
    assert (s.volume_cents, s.rate_cents) == (500, 0)


def test_vanished_line_item_is_all_volume():
    s = volume_rate_split(Cell(Fraction(500), Fraction(5)), Cell(Fraction(0), Fraction(0)))
    assert (s.volume_cents, s.rate_cents) == (-500, 0)


cells = st.builds(
    Cell,
    st.fractions(min_value=0, max_value=100_000, max_denominator=50),
    st.fractions(min_value=0, max_value=5_000, max_denominator=50),
)


@given(cells, cells)
def test_volume_plus_rate_is_always_the_total_change(before, after):
    s = volume_rate_split(before, after)
    assert s.total_cents == round_half_up(after.cost - before.cost)


# ----------------------------------------------------------------- contributions

row_strategy = st.builds(
    row,
    service=st.sampled_from(["EC2", "RDS", "S3", "NAT"]),
    region=st.sampled_from(["r1", "r2"]),
    usage=st.sampled_from(["U1", "U2", "U3"]),
    team=st.sampled_from(["", "a", "b"]),
    asg=st.sampled_from(["", "g1"]),
    rid=st.sampled_from(["x1", "x2", "x3", "x4"]),
    qty=st.floats(min_value=0.1, max_value=1000, allow_nan=False),
    cents=st.integers(min_value=0, max_value=200_000),
)


@settings(deadline=None, max_examples=150)
@given(
    st.lists(row_strategy, max_size=25),
    st.lists(st.lists(row_strategy, max_size=25), min_size=1, max_size=5),
)
def test_contributions_sum_to_total_change_in_every_dimension(today_rows, baseline_days):
    after = table_from_rows(today_rows)
    before = baseline_table([table_from_rows(d) for d in baseline_days])
    total = total_change(before, after)
    for dim in DIMENSIONS:
        assert sum(c.delta_cents for c in contributions(before, after, dim)) == total


@settings(deadline=None, max_examples=100)
@given(
    st.lists(row_strategy, min_size=1, max_size=25), st.lists(row_strategy, min_size=1, max_size=25)
)
def test_every_node_amount_equals_the_exact_change_inside_its_scope(today_rows, base_rows):
    """A cause can exceed the total (other values offset it), but its own amount is exact."""
    after, before = table_from_rows(today_rows), table_from_rows(base_rows)
    root = drill_down(before, after)
    assert root.delta_cents == total_change(before, after)

    def inside(table, scope):
        idx = {d: DIMENSIONS.index(d) for d in scope}
        return {k: c for k, c in table.items() if all(k[idx[d]] == v for d, v in scope.items())}

    def check(node):
        b, a = inside(before, node.scope), inside(after, node.scope)
        assert node.delta_cents == total_change(b, a)
        for child in node.children:
            assert set(node.scope.items()) <= set(child.scope.items())
            check(child)

    check(root)


def test_hand_made_example_adds_up_exactly():
    base = [row(service="EC2", rid="a", cents=1000), row(service="S3", rid="b", cents=500)]
    now = [
        row(service="EC2", rid="a", cents=3000),
        row(service="S3", rid="b", cents=450),
        row(service="RDS", rid="c", cents=700),
    ]
    before, after = table_from_rows(base), table_from_rows(now)
    by_service = {c.value: c.delta_cents for c in contributions(before, after, "service")}
    assert by_service == {"EC2": 2000, "S3": -50, "RDS": 700}
    assert sum(by_service.values()) == total_change(before, after) == 2650


def test_baseline_is_the_per_key_median_and_missing_days_count_as_zero():
    days = [table_from_rows([row(cents=c)]) for c in (100, 100, 5000)]
    assert next(iter(baseline_table(days).values())).cost == 100
    days = [table_from_rows([row(cents=100)]), table_from_rows([]), table_from_rows([])]
    assert next(iter(baseline_table(days).values())).cost == 0


def test_even_number_of_days_averages_the_middle_two():
    days = [table_from_rows([row(cents=c)]) for c in (100, 200, 300, 400)]
    assert next(iter(baseline_table(days).values())).cost == 250


# ----------------------------------------------------------------- drill-down


def test_drill_down_names_the_one_resource_behind_a_spike():
    base = [row(rid=f"i-{n}", cents=1000) for n in range(5)]
    now = [*base, row(rid="i-new", usage="Big", cents=4000)]
    root = drill_down(table_from_rows(base), table_from_rows(now))
    (cause,) = root.causes()
    assert cause.scope["resource_id"] == "i-new"
    assert cause.delta_cents == 4000 and cause.share == 1.0


def test_diffuse_change_is_reported_as_a_group_with_contributors():
    now = [row(rid=f"i-{n}", asg="g", cents=1000) for n in range(6)]
    root = drill_down({}, table_from_rows(now))
    (cause,) = root.causes()
    assert cause.scope.get("asg") == "g"
    assert cause.spread and len(cause.children) == 3  # top three contributors listed
    assert cause.delta_cents == 6000


def test_decrease_is_attributed_with_negative_amounts():
    base = [row(rid="a", cents=5000), row(rid="b", cents=1000)]
    now = [row(rid="a", cents=1000), row(rid="b", cents=1000)]
    root = drill_down(table_from_rows(base), table_from_rows(now))
    assert root.delta_cents == -4000
    assert root.causes()[0].scope["resource_id"] == "a"


def test_no_change_gives_no_children():
    t = table_from_rows([row()])
    root = drill_down(t, t)
    assert root.delta_cents == 0 and root.children == []


def test_rate_effect_is_found_at_the_resource():
    base = [row(rid="db", usage="Inst", qty=24, cents=2400), row(rid="o", cents=100)]
    now = [row(rid="db", usage="Inst", qty=24, cents=6000), row(rid="o", cents=100)]
    root = drill_down(table_from_rows(base), table_from_rows(now))
    cause = root.causes()[0]
    assert cause.scope["resource_id"] == "db"
    assert cause.split.rate_cents == 3600 and cause.split.volume_cents == 0


def test_usage_rows_drops_tax_credit_and_refund_but_keeps_fees():
    rows = [row(), row(line="Tax"), row(line="Credit"), row(line="Refund"), row(line="Fee")]
    assert [r.line_type for r in usage_rows(rows)] == ["Usage", "Fee"]


def test_drill_down_is_deterministic():
    base = [row(rid=f"i-{n}", cents=1000 + n) for n in range(8)]
    now = [row(rid=f"i-{n}", cents=1000 + 3 * n) for n in range(8)]
    a = drill_down(table_from_rows(base), table_from_rows(now))
    b = drill_down(table_from_rows(base), table_from_rows(now))
    assert [c.scope for c in a.causes()] == [c.scope for c in b.causes()]
