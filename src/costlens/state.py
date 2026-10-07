"""App state in SQLite: day reports, explanations, alert acknowledgements, waste outcomes.

SQLite keeps the MVP free of a database server; the schema is plain SQL and moves to
Postgres unchanged apart from the placeholder style.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .explain import Explanation
from .report import DayReport
from .waste.rules import Finding

SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    day TEXT PRIMARY KEY,
    total_cents INTEGER NOT NULL,
    baseline_cents INTEGER NOT NULL,
    delta_cents INTEGER NOT NULL,
    pct REAL,
    significant INTEGER NOT NULL,
    warnings TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS explanations (
    day TEXT NOT NULL,
    facts_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    facts_json TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    digest_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (day, facts_hash)
);
CREATE TABLE IF NOT EXISTS alerts (
    alert_key TEXT PRIMARY KEY,
    status TEXT NOT NULL,            -- open | acked | snoozed
    snooze_until TEXT,
    updated_by TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    rule TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    status TEXT NOT NULL,            -- open | fixed | snoozed | dismissed
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved_on TEXT,
    monthly_savings_cents INTEGER NOT NULL,
    savings_source TEXT NOT NULL,
    summary TEXT NOT NULL,
    fix TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    PRIMARY KEY (rule, resource_id)
);
"""


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


class State:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ days

    def save_day(
        self,
        report: DayReport,
        facts: dict[str, Any],
        exp: Explanation,
        digest_text: str,
        now: datetime | None = None,
    ) -> None:
        """Store a day and its explanation together, so every sentence traces to its facts."""
        now = now or datetime.now(UTC)
        day = report.day.isoformat()
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO days VALUES (?,?,?,?,?,?,?,?)",
                (
                    day,
                    report.total_cents,
                    report.baseline_cents,
                    report.delta_cents,
                    report.pct,
                    int(report.significant),
                    json.dumps(report.warnings),
                    _iso(now),
                ),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO explanations VALUES (?,?,?,?,?,?,?)",
                (
                    day,
                    exp.facts_hash,
                    exp.source,
                    json.dumps(facts, sort_keys=True),
                    json.dumps(exp.as_dict(), sort_keys=True),
                    digest_text,
                    _iso(now),
                ),
            )

    def list_days(self, limit: int = 90) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM days ORDER BY day DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["significant"] = bool(d["significant"])
            d["warnings"] = json.loads(d["warnings"])
            out.append(d)
        return out

    def explanations(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM explanations ORDER BY created_at DESC, day DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["facts"] = json.loads(d.pop("facts_json"))
            d["explanation"] = json.loads(d.pop("explanation_json"))
            out.append(d)
        return out

    def explanation_for(self, day: date) -> dict[str, Any] | None:
        rows = [e for e in self.explanations(500) if e["day"] == day.isoformat()]
        return rows[0] if rows else None

    # ------------------------------------------------------------------ alerts

    def set_alert(
        self, key: str, status: str, user: str, now: datetime, snooze_for: timedelta | None = None
    ) -> None:
        until = _iso(now + snooze_for) if snooze_for else None
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO alerts VALUES (?,?,?,?,?)",
                (key, status, until, user, _iso(now)),
            )

    def alert(self, key: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM alerts WHERE alert_key=?", (key,)).fetchone()
        return dict(r) if r else None

    def should_alert(self, key: str, now: datetime) -> bool:
        """False while the alert is acknowledged for today or snoozed."""
        a = self.alert(key)
        if not a:
            return True
        if a["status"] == "snoozed" and a["snooze_until"]:
            return now >= datetime.fromisoformat(a["snooze_until"])
        if a["status"] == "acked":
            return now.date() > datetime.fromisoformat(a["updated_at"]).date()
        return True

    # ------------------------------------------------------------------ waste findings

    def record_findings(self, findings: Iterable[Finding], today: date) -> None:
        """Upsert today's findings. An open finding that vanished is recorded as fixed."""
        seen: set[tuple[str, str]] = set()
        with self.db:
            for f in findings:
                seen.add((f.rule, f.resource_id))
                existing = self.db.execute(
                    "SELECT status FROM findings WHERE rule=? AND resource_id=?",
                    (f.rule, f.resource_id),
                ).fetchone()
                if existing is None:
                    self.db.execute(
                        "INSERT INTO findings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            f.rule,
                            f.resource_id,
                            "open",
                            today.isoformat(),
                            today.isoformat(),
                            None,
                            f.monthly_savings_cents,
                            f.savings_source,
                            f.summary,
                            f.fix,
                            json.dumps(f.evidence, sort_keys=True),
                        ),
                    )
                else:
                    status = "open" if existing["status"] == "fixed" else existing["status"]
                    self.db.execute(
                        "UPDATE findings SET last_seen=?, status=?, resolved_on=NULL, "
                        "monthly_savings_cents=?, savings_source=?, summary=?, fix=?, "
                        "evidence_json=? WHERE rule=? AND resource_id=?",
                        (
                            today.isoformat(),
                            status,
                            f.monthly_savings_cents,
                            f.savings_source,
                            f.summary,
                            f.fix,
                            json.dumps(f.evidence, sort_keys=True),
                            f.rule,
                            f.resource_id,
                        ),
                    )
            for r in self.db.execute("SELECT rule, resource_id FROM findings WHERE status='open'"):
                if (r["rule"], r["resource_id"]) not in seen:
                    self.db.execute(
                        "UPDATE findings SET status='fixed', resolved_on=? "
                        "WHERE rule=? AND resource_id=?",
                        (today.isoformat(), r["rule"], r["resource_id"]),
                    )

    def set_finding_status(self, rule: str, resource_id: str, status: str) -> bool:
        if status not in ("open", "snoozed", "dismissed", "fixed"):
            raise ValueError(f"bad status {status!r}")
        with self.db:
            cur = self.db.execute(
                "UPDATE findings SET status=? WHERE rule=? AND resource_id=?",
                (status, rule, resource_id),
            )
        return cur.rowcount > 0

    def findings(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM findings"
        args: tuple = ()
        if status:
            sql += " WHERE status=?"
            args = (status,)
        rows = self.db.execute(sql + " ORDER BY monthly_savings_cents DESC", args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["evidence"] = json.loads(d.pop("evidence_json"))
            out.append(d)
        return out

    def savings_summary(self) -> dict[str, int]:
        """Monthly savings by outcome. Only CUR-priced fixed findings count as 'saved'."""

        def total(where: str) -> int:
            return self.db.execute(
                f"SELECT COALESCE(SUM(monthly_savings_cents),0) FROM findings WHERE {where}"
            ).fetchone()[0]

        return {
            "open_cents": total("status='open'"),
            "saved_cents": total("status='fixed' AND savings_source='cur'"),
            "saved_estimated_cents": total("status='fixed' AND savings_source!='cur'"),
            "dismissed_cents": total("status='dismissed'"),
            "snoozed_cents": total("status='snoozed'"),
        }
