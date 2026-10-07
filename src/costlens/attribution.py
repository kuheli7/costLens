"""Deterministic change attribution: exact, additive causes for a cost change.

Nothing here touches AWS or an LLM. Every number the rest of the system reports comes
from this module, and every contribution sums to the measured total change to the cent.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from math import log

from .models import DIMENSIONS, NON_USAGE_LINE_TYPES, CostRow
from .money import allocate_rounded, round_half_up

Key = tuple[str, str, str, str, str, str]  # CostRow.key


@dataclass(frozen=True, slots=True)
class Cell:
    """Cost and usage at the finest granularity. Values may be fractional (baselines)."""

    cost: Fraction
    qty: Fraction

    def __add__(self, other: Cell) -> Cell:
        return Cell(self.cost + other.cost, self.qty + other.qty)


ZERO = Cell(Fraction(0), Fraction(0))
Table = dict[Key, Cell]


def usage_rows(rows: Iterable[CostRow]) -> list[CostRow]:
    """Rows that count as usage (drops tax, credits, refunds)."""
    return [r for r in rows if r.line_type not in NON_USAGE_LINE_TYPES]


def table_from_rows(rows: Iterable[CostRow]) -> Table:
    out: dict[Key, Cell] = {}
    for r in rows:
        cell = Cell(Fraction(r.cost_cents), Fraction(r.usage_qty).limit_denominator(10**9))
        out[r.key] = out.get(r.key, ZERO) + cell
    return out


def baseline_table(per_day_tables: Sequence[Table]) -> Table:
    """Per-key median across days. Days where a key is absent count as zero.

    The baseline total is defined as the sum of these per-key medians, which is what makes
    the later split additive (a median of totals would not equal the sum of medians).
    """
    keys = {k for t in per_day_tables for k in t}
    n = len(per_day_tables)
    out: Table = {}
    for k in keys:
        costs = sorted(t.get(k, ZERO).cost for t in per_day_tables)
        qtys = sorted(t.get(k, ZERO).qty for t in per_day_tables)
        out[k] = Cell(_median(costs, n), _median(qtys, n))
    return out


def _median(sorted_vals: Sequence[Fraction], n: int) -> Fraction:
    if n == 0:
        return Fraction(0)
    mid = n // 2
    if n % 2:
        return sorted_vals[mid]
    return (sorted_vals[mid - 1] + sorted_vals[mid]) / 2


def _dim_index(name: str) -> int:
    return DIMENSIONS.index(name)


def _sum_cost(table: Table) -> Fraction:
    return sum((c.cost for c in table.values()), Fraction(0))


def _sum_qty(table: Table) -> Fraction:
    return sum((c.qty for c in table.values()), Fraction(0))


# --------------------------------------------------------------------------- volume / rate


@dataclass(frozen=True, slots=True)
class VolumeRate:
    volume_cents: int
    rate_cents: int
    qty_before: float
    qty_after: float

    @property
    def total_cents(self) -> int:
        return self.volume_cents + self.rate_cents


def volume_rate_split(before: Cell, after: Cell) -> VolumeRate:
    """Split a change into a volume effect and a rate (unit price) effect.

        delta = (q1 - q0) * p0  +  (p1 - p0) * q1

    Edge cases: a brand-new line item (q0 = 0) is all volume; a line item that vanished
    (q1 = 0) is all volume. The rate effect is computed as a residual of the rounded volume
    effect, so volume + rate equals the total change exactly, in integer cents.
    """
    total = round_half_up(after.cost - before.cost)
    if before.qty == 0 or after.qty == 0:
        return VolumeRate(total, 0, float(before.qty), float(after.qty))
    p0 = before.cost / before.qty
    volume = round_half_up((after.qty - before.qty) * p0)
    return VolumeRate(volume, total - volume, float(before.qty), float(after.qty))


# --------------------------------------------------------------------------- contributions


@dataclass(frozen=True, slots=True)
class Contribution:
    value: str
    before_cents: Fraction
    after_cents: Fraction
    delta_cents: int  # rounded so the contributions of one dimension sum to the total


def contributions(before: Table, after: Table, dim: str) -> list[Contribution]:
    """Per-value change along one dimension. Sum of `delta_cents` == total change, exactly."""
    idx = _dim_index(dim)
    b: dict[str, Fraction] = defaultdict(Fraction)
    a: dict[str, Fraction] = defaultdict(Fraction)
    for k, cell in before.items():
        b[k[idx]] += cell.cost
    for k, cell in after.items():
        a[k[idx]] += cell.cost
    values = sorted(set(b) | set(a))
    deltas = [a[v] - b[v] for v in values]
    rounded = allocate_rounded(deltas)
    return [Contribution(v, b[v], a[v], d) for v, d in zip(values, rounded, strict=True)]


def total_change(before: Table, after: Table) -> int:
    return round_half_up(_sum_cost(after) - _sum_cost(before))


# --------------------------------------------------------------------------- drill-down


@dataclass(slots=True)
class CauseNode:
    scope: dict[str, str]  # dimension -> value this node is restricted to
    delta_cents: int
    share: float  # share of the top-level total change
    before_cents: int
    after_cents: int
    split: VolumeRate | None = None  # only when the node is a single usage type
    children: list[CauseNode] = field(default_factory=list)
    # True when the change is spread thinly over many resources: the children are then the
    # top contributors to this group, not separate causes.
    spread: bool = False

    def causes(self) -> list[CauseNode]:
        """The most specific nodes that are real causes, biggest change first.

        A branch whose change is spread thinly over many values (five new instances in one
        group) is reported as the branch itself, with its children as contributors.
        """
        if self.spread:
            return [self]
        found: list[CauseNode] = []
        for c in self.children:
            found.extend(c._cause_nodes())
        found.sort(key=lambda n: -abs(n.delta_cents))
        return found or [self]

    def _cause_nodes(self) -> list[CauseNode]:
        if self.is_leaf or self.spread:
            return [self]
        return [n for c in self.children for n in c._cause_nodes()]

    @property
    def is_leaf(self) -> bool:
        return not self.children

    def leaves(self) -> list[CauseNode]:
        if self.is_leaf:
            return [self]
        return [leaf for c in self.children for leaf in c.leaves()]


def _filter(table: Table, scope: dict[str, str]) -> Table:
    checks = [(_dim_index(d), v) for d, v in scope.items()]
    return {k: c for k, c in table.items() if all(k[i] == v for i, v in checks)}


def _surprise(p_base: float, p_now: float) -> float:
    """Jensen-Shannon divergence term between a value's baseline share and current share."""
    m = (p_base + p_now) / 2
    s = 0.0
    if p_base > 0 and m > 0:
        s += p_base * log(p_base / m)
    if p_now > 0 and m > 0:
        s += p_now * log(p_now / m)
    return s / 2


@dataclass(frozen=True, slots=True)
class _Candidate:
    dim: str
    values: tuple[str, ...]
    covered: float
    surprise: float


def _best_dimension(
    before: Table, after: Table, dims: Sequence[str], total: int, explain: float, max_values: int
) -> _Candidate | None:
    """Pick the dimension where the fewest values explain the most of the change."""
    if total == 0:
        return None
    sign = 1 if total > 0 else -1
    best: _Candidate | None = None
    b_total = _sum_cost(before)
    a_total = _sum_cost(after)
    for dim in dims:
        contribs = contributions(before, after, dim)
        pool = sorted(
            (c for c in contribs if c.delta_cents * sign > 0), key=lambda c: -abs(c.delta_cents)
        )
        chosen: list[Contribution] = []
        covered = 0.0
        for c in pool[:max_values]:
            chosen.append(c)
            covered += c.delta_cents / total
            if covered >= explain:
                break
        if not chosen:
            continue
        surprise = sum(
            _surprise(
                float(c.before_cents / b_total) if b_total else 0.0,
                float(c.after_cents / a_total) if a_total else 0.0,
            )
            for c in chosen
        )
        cand = _Candidate(dim, tuple(c.value for c in chosen), covered, surprise)
        if best is None or _better(cand, best, explain):
            best = cand
    return best


def _better(a: _Candidate, b: _Candidate, explain: float) -> bool:
    a_ok, b_ok = a.covered >= explain, b.covered >= explain
    if a_ok != b_ok:
        return a_ok
    if len(a.values) != len(b.values):
        return len(a.values) < len(b.values)
    if abs(a.covered - b.covered) > 1e-9:
        return a.covered > b.covered
    # equal on coverage: higher surprise wins; exact ties keep the coarser dimension
    return a.surprise > b.surprise + 1e-12


def drill_down(
    before: Table,
    after: Table,
    explain: float = 0.67,
    max_values: int = 3,
    min_share: float = 0.05,
    max_depth: int = 6,
    dims: Sequence[str] = DIMENSIONS,
) -> CauseNode:
    """Build a tree of causes with exact dollar amounts.

    Root = the total change. At each node, pick the dimension where the fewest values explain
    the change, recurse into those values, and stop when no dimension separates the change
    any further or a branch is below `min_share` of the overall change.
    """
    total = total_change(before, after)
    root = _make_node(before, after, {}, total, total)
    _expand(root, before, after, total, explain, max_values, min_share, max_depth, dims, 0)
    return root


def _make_node(
    before: Table, after: Table, scope: dict[str, str], delta: int, overall: int
) -> CauseNode:
    b, a = _sum_cost(before), _sum_cost(after)
    # Record dimensions the node pins down implicitly (a single value across the node).
    full_scope = dict(scope)
    for dim in DIMENSIONS:
        if dim in full_scope:
            continue
        idx = _dim_index(dim)
        vals = {k[idx] for k in (*before, *after)}
        if len(vals) == 1:
            full_scope[dim] = next(iter(vals))
    split = None
    if "usage_type" in full_scope:
        split = volume_rate_split(Cell(b, _sum_qty(before)), Cell(a, _sum_qty(after)))
    return CauseNode(
        scope=full_scope,
        delta_cents=delta,
        share=(delta / overall) if overall else 0.0,
        before_cents=round_half_up(b),
        after_cents=round_half_up(a),
        split=split,
    )


def _expand(
    node: CauseNode,
    before: Table,
    after: Table,
    overall: int,
    explain: float,
    max_values: int,
    min_share: float,
    max_depth: int,
    dims: Sequence[str],
    depth: int,
) -> None:
    # A dimension where one value carries the whole change narrows this node in place
    # instead of adding a child, so the tree only branches where the change really splits.
    while True:
        free = [d for d in dims if d not in node.scope]
        cand = _best_dimension(before, after, free, node.delta_cents, explain, max_values)
        if cand is None:
            return
        if len(cand.values) == 1:
            scope = {**node.scope, cand.dim: cand.values[0]}
            sb, sa = _filter(before, scope), _filter(after, scope)
            if round_half_up(_sum_cost(sa) - _sum_cost(sb)) == node.delta_cents:
                narrowed = _make_node(sb, sa, scope, node.delta_cents, overall)
                node.scope, node.before_cents = narrowed.scope, narrowed.before_cents
                node.after_cents, node.split = narrowed.after_cents, narrowed.split
                before, after = sb, sa
                continue
        break
    if depth >= max_depth:
        return
    node.spread = cand.dim == "resource_id" and cand.covered < explain
    for value in cand.values:
        sub_scope = {**node.scope, cand.dim: value}
        sb, sa = _filter(before, sub_scope), _filter(after, sub_scope)
        delta = round_half_up(_sum_cost(sa) - _sum_cost(sb))
        if overall and abs(delta / overall) < min_share:
            continue
        child = _make_node(sb, sa, sub_scope, delta, overall)
        node.children.append(child)
        _expand(child, sb, sa, overall, explain, max_values, min_share, max_depth, dims, depth + 1)
