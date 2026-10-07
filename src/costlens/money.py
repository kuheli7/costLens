"""Exact money helpers. Amounts are integer cents; fractional values are `Fraction`."""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction


def round_half_up(x: Fraction) -> int:
    """Round a Fraction to the nearest integer, halves away from zero."""
    if x >= 0:
        return int((x + Fraction(1, 2)).__floor__())
    return -int((-x + Fraction(1, 2)).__floor__())


def allocate_rounded(parts: Sequence[Fraction]) -> list[int]:
    """Round each part to an integer so the rounded parts sum to round(sum(parts)).

    Largest-remainder method: floor everything, then give the leftover units to the
    parts with the largest fractional remainders (ties broken by position, so the
    result is deterministic).
    """
    target = round_half_up(sum(parts, Fraction(0)))
    floors = [p.__floor__() for p in parts]
    remainders = [p - f for p, f in zip(parts, floors, strict=True)]
    leftover = target - sum(floors)
    order = sorted(range(len(parts)), key=lambda i: (-remainders[i], i))
    out = list(floors)
    step = 1 if leftover >= 0 else -1
    # leftover can only be negative when round_half_up rounded a sum down past the floors,
    # which cannot happen for floors, but handle it so the invariant is unconditional.
    for k in range(abs(leftover)):
        idx = order[k % len(order)] if step > 0 else order[-1 - (k % len(order))]
        out[idx] += step
    return out


def fmt_usd(cents: int, sign: bool = False) -> str:
    """Format integer cents as dollars, e.g. 14230 -> '$142.30'."""
    neg = cents < 0
    c = abs(cents)
    body = f"${c // 100:,}.{c % 100:02d}"
    if neg:
        return f"-{body}"
    return f"+{body}" if sign and cents > 0 else body
