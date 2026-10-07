"""Row storage with idempotent daily loads.

Re-running a day replaces that day; it never duplicates it. Two backends share one contract:
DuckStore (a local DuckDB file or memory, for development and tests) and S3ParquetStore
(date-partitioned Parquet that Athena or DuckDB can query in place).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Any, Protocol

import duckdb

from .models import CostRow

_COLUMNS = (
    "day DATE, service VARCHAR, region VARCHAR, usage_type VARCHAR, resource_id VARCHAR, "
    "team VARCHAR, asg VARCHAR, usage_qty DOUBLE, cost_cents BIGINT, line_type VARCHAR"
)
_FIELDS = (
    "day, service, region, usage_type, resource_id, team, asg, usage_qty, cost_cents, line_type"
)


def _tuple(r: CostRow) -> tuple:
    return (
        r.day, r.service, r.region, r.usage_type, r.resource_id, r.team, r.asg, r.usage_qty,
        r.cost_cents, r.line_type,
    )  # fmt: skip


def _row(t: tuple) -> CostRow:
    d = t[0]
    return CostRow(
        d if isinstance(d, date) else date.fromisoformat(str(d)),
        t[1], t[2], t[3], t[4], t[5], t[6], float(t[7]), int(t[8]), t[9],
    )  # fmt: skip


class RowStore(Protocol):
    def load_day(self, day: date, rows: Sequence[CostRow]) -> None: ...
    def read_range(self, start: date, end: date) -> list[CostRow]: ...
    def days(self) -> list[date]: ...


class DuckStore:
    def __init__(self, path: str = ":memory:"):
        self.con = duckdb.connect(path)
        self.con.execute(f"CREATE TABLE IF NOT EXISTS cost_lines ({_COLUMNS})")
        # Separate record of loaded days, so an empty day is distinguishable from an unloaded one.
        self.con.execute("CREATE TABLE IF NOT EXISTS loaded_days (day DATE PRIMARY KEY)")

    def load_day(self, day: date, rows: Sequence[CostRow]) -> None:
        wrong = [r for r in rows if r.day != day]
        if wrong:
            raise ValueError(f"{len(wrong)} rows are not for {day}")
        self.con.execute("BEGIN")
        try:
            self.con.execute("DELETE FROM cost_lines WHERE day = ?", [day])
            self.con.execute("DELETE FROM loaded_days WHERE day = ?", [day])
            self.con.execute("INSERT INTO loaded_days VALUES (?)", [day])
            if rows:
                self.con.executemany(
                    f"INSERT INTO cost_lines ({_FIELDS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [_tuple(r) for r in rows],
                )
            self.con.execute("COMMIT")
        except Exception:
            self.con.execute("ROLLBACK")
            raise

    def read_range(self, start: date, end: date) -> list[CostRow]:
        recs = self.con.execute(
            f"SELECT {_FIELDS} FROM cost_lines WHERE day BETWEEN ? AND ? "
            "ORDER BY day, service, region, usage_type, resource_id, team, asg, line_type",
            [start, end],
        ).fetchall()
        return [_row(r) for r in recs]

    def days(self) -> list[date]:
        return [
            r[0] for r in self.con.execute("SELECT day FROM loaded_days ORDER BY day").fetchall()
        ]

    def close(self) -> None:
        self.con.close()


def write_parquet(rows: Sequence[CostRow], path: str) -> None:
    con = duckdb.connect()
    try:
        con.execute(f"CREATE TABLE t ({_COLUMNS})")
        if rows:
            con.executemany(
                f"INSERT INTO t ({_FIELDS}) VALUES (?,?,?,?,?,?,?,?,?,?)", [_tuple(r) for r in rows]
            )
        con.execute(f"COPY t TO '{path.replace(chr(39), chr(39) * 2)}' (FORMAT PARQUET)")
    finally:
        con.close()


def read_parquet(paths: Sequence[str]) -> list[CostRow]:
    if not paths:
        return []
    con = duckdb.connect()
    try:
        listing = ", ".join("'" + p.replace("'", "''") + "'" for p in paths)
        recs = con.execute(
            f"SELECT {_FIELDS} FROM read_parquet([{listing}]) "
            "ORDER BY day, service, region, usage_type, resource_id, team, asg, line_type"
        ).fetchall()
        return [_row(r) for r in recs]
    finally:
        con.close()


class S3ParquetStore:
    """One Parquet object per day at `<prefix>/date=YYYY-MM-DD/part.parquet`.

    A load overwrites that day's single object, so re-running a day replaces it (S3 PUT of an
    existing key is atomic for readers). A day with no rows still writes an empty file, so
    'no data' is distinguishable from 'not loaded yet'.
    """

    def __init__(self, s3_client: Any, bucket: str, prefix: str = "normalized"):
        self._s3, self.bucket, self.prefix = s3_client, bucket, prefix.strip("/")

    def key(self, day: date) -> str:
        return f"{self.prefix}/date={day.isoformat()}/part.parquet"

    def load_day(self, day: date, rows: Sequence[CostRow]) -> None:
        wrong = [r for r in rows if r.day != day]
        if wrong:
            raise ValueError(f"{len(wrong)} rows are not for {day}")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "part.parquet")
            write_parquet(rows, path)
            self._s3.upload_file(path, self.bucket, self.key(day))

    def days(self) -> list[date]:
        found: set[date] = set()
        for page in self._s3.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=self.prefix + "/"
        ):
            for obj in page.get("Contents", []):
                part = obj["Key"].split("/")[-2]
                if part.startswith("date="):
                    found.add(date.fromisoformat(part[5:]))
        return sorted(found)

    def read_range(self, start: date, end: date) -> list[CostRow]:
        wanted = [d for d in self.days() if start <= d <= end]
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for d in wanted:
                p = os.path.join(tmp, f"{d.isoformat()}.parquet")
                self._s3.download_file(self.bucket, self.key(d), p)
                paths.append(p)
            return read_parquet(paths)


def history_window(day: date, days: int = 35) -> tuple[date, date]:
    """Range of history the analysis needs: four same-weekday points plus slack."""
    return day - timedelta(days=days), day
