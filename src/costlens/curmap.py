"""Mapping from AWS Cost and Usage Report 2.0 rows to CostRow. The ONE place that knows CUR names.

STATUS OF THESE NAMES (read before trusting them against a real export):

* Confirmed from the AWS CUR 2.0 documentation page "table-dictionary-cur2": the SQL table is
  ``COST_AND_USAGE_REPORT``; ``line_item_resource_id`` exists when the export enables
  INCLUDE_RESOURCES; granularity is HOURLY, DAILY or MONTHLY; resource tags are nested in a
  key-value column rather than one column per tag.
* NOT verified against a real export or a column-level page (the other column names below come
  from the legacy CUR schema, which CUR 2.0 is documented to keep): ``line_item_usage_start_date``,
  ``line_item_usage_amount``, ``line_item_unblended_cost``, ``line_item_product_code``,
  ``product_region_code``, ``line_item_usage_type``, ``line_item_line_item_type``, and the name
  and key format of the tags column. Check them against the first real file and adjust
  ``CurColumns`` / ``TAG_KEYS``; nothing outside this module needs to change.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .models import CostRow


@dataclass(frozen=True, slots=True)
class CurColumns:
    start: str = "line_item_usage_start_date"
    cost: str = "line_item_unblended_cost"
    usage: str = "line_item_usage_amount"
    resource: str = "line_item_resource_id"
    service: str = "line_item_product_code"
    region: tuple[str, ...] = ("product_region_code", "product_region")
    usage_type: str = "line_item_usage_type"
    line_type: str = "line_item_line_item_type"
    tags: tuple[str, ...] = ("tags", "resource_tags")


COLUMNS = CurColumns()

# Candidate keys inside the tags map, first match wins. Legacy CUR flattened tag names to
# "user_<name>" and "aws_<name>"; the CUR 2.0 key format is unverified, so accept several.
TAG_KEYS = {
    "team": ("user_team", "team", "user:team", "resourceTags/user:team"),
    "asg": (
        "aws_autoscaling_groupName",
        "aws:autoscaling:groupName",
        "aws_autoscaling_groupname",
        "resourceTags/aws:autoscaling:groupName",
    ),
}

_LINE_TYPES = {
    "Usage": "Usage",
    "DiscountedUsage": "Usage",
    "SavingsPlanCoveredUsage": "Usage",
    "SavingsPlanNegation": "Usage",
    "Tax": "Tax",
    "Credit": "Credit",
    "Refund": "Refund",
    "EdpDiscount": "Credit",
    "BundledDiscount": "Credit",
    "SppDiscount": "Credit",
    "PrivateRateDiscount": "Credit",
    "Fee": "Fee",
    "RIFee": "Fee",
    "SavingsPlanRecurringFee": "Fee",
    "SavingsPlanUpfrontFee": "Fee",
}


def normalise_line_type(raw: str | None) -> str:
    """Map a CUR line item type to Usage | Tax | Credit | Refund | Fee. Unknown types are Usage."""
    return _LINE_TYPES.get((raw or "Usage").strip(), "Usage")


def tag_lookup(raw: Any, candidates: Sequence[str]) -> str:
    """Read one tag out of whatever shape the tags column arrives in.

    Handles a dict, a DuckDB map ({'key': [...], 'value': [...]}), a list of {key, value}
    pairs, a JSON string, and None.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return ""
    flat: Mapping[str, Any]
    if isinstance(raw, Mapping) and set(raw) == {"key", "value"} and isinstance(raw["key"], list):
        flat = dict(zip(raw["key"], raw["value"], strict=False))
    elif isinstance(raw, Mapping):
        flat = raw
    elif isinstance(raw, list):
        flat = {p.get("key"): p.get("value") for p in raw if isinstance(p, Mapping)}
    else:
        return ""
    for name in candidates:
        value = flat.get(name)
        if value:
            return str(value)
    return ""


def _first(row: Mapping[str, Any], names: Iterable[str]) -> Any:
    for n in names:
        if n in row and row[n] is not None:
            return row[n]
    return None


def _day(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def _cents(dollars: Decimal) -> int:
    return int((dollars * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def rows_from_cur(
    raw_rows: Iterable[Mapping[str, Any]], cols: CurColumns = COLUMNS
) -> list[CostRow]:
    """Aggregate CUR line items (any granularity) into daily CostRows.

    Costs are summed as Decimal dollars per group and converted to integer cents once, so
    hourly rows never accumulate float or rounding drift.
    """
    groups: dict[tuple, list] = defaultdict(lambda: [Decimal(0), Decimal(0)])
    for r in raw_rows:
        day = _day(r[cols.start])
        region = _first(r, cols.region) or ""
        team = tag_lookup(_first(r, cols.tags), TAG_KEYS["team"])
        asg = tag_lookup(_first(r, cols.tags), TAG_KEYS["asg"])
        key = (
            day,
            str(r.get(cols.service) or ""),
            str(region),
            str(r.get(cols.usage_type) or ""),
            str(r.get(cols.resource) or ""),
            team,
            asg,
            normalise_line_type(r.get(cols.line_type)),
        )
        g = groups[key]
        g[0] += Decimal(str(r.get(cols.cost) or 0))
        g[1] += Decimal(str(r.get(cols.usage) or 0))
    return [
        CostRow(day, svc, region, ut, rid, team, asg, float(qty), _cents(cost), lt)
        for (day, svc, region, ut, rid, team, asg, lt), (cost, qty) in sorted(groups.items())
    ]


def read_cur_parquet(paths: Sequence[str], cols: CurColumns = COLUMNS) -> list[CostRow]:
    """Read CUR Parquet files with DuckDB and aggregate them into daily CostRows."""
    import duckdb

    con = duckdb.connect()
    try:
        listing = ", ".join("'" + p.replace("'", "''") + "'" for p in paths)
        available = {
            r[0]
            for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet([{listing}])").fetchall()
        }
        wanted = [
            cols.start, cols.cost, cols.usage, cols.resource, cols.service, cols.usage_type,
            cols.line_type, *cols.region, *cols.tags,
        ]  # fmt: skip
        select = ", ".join(f'"{c}"' for c in wanted if c in available)
        missing = [c for c in (cols.start, cols.cost, cols.service) if c not in available]
        if missing:
            raise ValueError(f"CUR file is missing expected columns {missing}; see curmap.py")
        cur = con.execute(f"SELECT {select} FROM read_parquet([{listing}])")
        names = [d[0] for d in cur.description]
        return rows_from_cur((dict(zip(names, rec, strict=True)) for rec in cur.fetchall()), cols)
    finally:
        con.close()
