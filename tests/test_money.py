from fractions import Fraction

from hypothesis import given
from hypothesis import strategies as st

from costlens.money import allocate_rounded, fmt_usd, round_half_up

fractions = st.fractions(min_value=-10_000, max_value=10_000, max_denominator=997)


@given(st.lists(fractions, min_size=1, max_size=30))
def test_rounded_parts_sum_to_rounded_total(parts):
    out = allocate_rounded(parts)
    assert sum(out) == round_half_up(sum(parts, Fraction(0)))


@given(st.lists(fractions, min_size=1, max_size=30))
def test_each_part_within_one_unit_of_its_exact_value(parts):
    out = allocate_rounded(parts)
    assert all(abs(o - p) <= 1 for o, p in zip(out, parts, strict=True))


def test_thirds_do_not_lose_a_cent():
    assert sum(allocate_rounded([Fraction(1, 3)] * 3)) == 1


def test_round_half_up_is_symmetric():
    assert round_half_up(Fraction(5, 2)) == 3
    assert round_half_up(Fraction(-5, 2)) == -3
    assert round_half_up(Fraction(1, 3)) == 0


def test_fmt_usd():
    assert fmt_usd(14230) == "$142.30"
    assert fmt_usd(-5) == "-$0.05"
    assert fmt_usd(3810, sign=True) == "+$38.10"
    assert fmt_usd(123456789) == "$1,234,567.89"
