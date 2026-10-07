"""AWS Lambda entry points. Thin wrappers: all logic lives in tested modules.

`ingest`  loads CUR rows for a day (and the three days before it, because CUR revises a day for
          up to 72 hours) into the Parquet store, and saves the Cost Explorer total beside it.
`digest`  analyses a day, writes the explanation, and posts it to Slack.

Both take their AWS clients through `Deps`, so tests run them against moto and stubs.
The Lambda-facing functions (`lambda_ingest`, `lambda_digest`) only build `Deps` from the
environment. Nothing here writes to AWS other than the tool's own S3 bucket.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from . import ce as ce_module
from .curmap import read_cur_parquet
from .explain import AnthropicWriter, TokenBudget, Writer
from .pipeline import run_day
from .slack import HttpPost, post_message
from .state import State
from .store import S3ParquetStore, history_window
from .waste import CloudWatchMetrics, PriceBook, WasteConfig, collect, run_all

REVISION_DAYS = 3  # CUR can revise a day for up to 72 hours, so reload this many earlier days
CLOUDTRAIL_PAD = timedelta(days=1)


@dataclass(slots=True)
class Config:
    bucket: str
    cur_prefix: str = "cur"
    normalized_prefix: str = "normalized"
    raw_prefix: str = "raw"
    state_key: str = "state/costlens.sqlite"
    slack_channel: str = ""
    ai: bool = False
    token_budget: int = 200_000
    waste: bool = True
    details_url: str | None = None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Config:
        e = env if env is not None else os.environ
        return cls(
            bucket=e["COSTLENS_BUCKET"],
            cur_prefix=e.get("COSTLENS_CUR_PREFIX", "cur"),
            normalized_prefix=e.get("COSTLENS_NORMALIZED_PREFIX", "normalized"),
            raw_prefix=e.get("COSTLENS_RAW_PREFIX", "raw"),
            state_key=e.get("COSTLENS_STATE_KEY", "state/costlens.sqlite"),
            slack_channel=e.get("COSTLENS_SLACK_CHANNEL", ""),
            ai=e.get("COSTLENS_AI", "off").lower() == "on",
            token_budget=int(e.get("COSTLENS_TOKEN_BUDGET", "200000")),
            waste=e.get("COSTLENS_WASTE", "on").lower() == "on",
            details_url=e.get("COSTLENS_DETAILS_URL") or None,
        )


@dataclass(slots=True)
class Deps:
    s3: Any
    ce: Any = None
    cloudtrail: Any = None
    session: Any = None  # boto3 Session, for the read-only waste inventory
    secrets: Any = None
    http_post: HttpPost | None = None
    writer: Writer | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))


def yesterday(now: datetime) -> date:
    return (now - timedelta(days=1)).date()


def _target_day(event: dict[str, Any], now: datetime) -> date:
    raw = (event or {}).get("date")
    return date.fromisoformat(raw) if raw else yesterday(now)


# --------------------------------------------------------------------------- ingest


def list_cur_objects(s3: Any, bucket: str, prefix: str, day: date) -> list[str]:
    """Parquet keys of the CUR export for the day's billing month.

    ASSUMPTION (unverified against a real export): Data Exports writes
    ``<prefix>/<export-name>/data/BILLING_PERIOD=YYYY-MM/*.parquet``. This matches any key
    under the prefix that contains ``BILLING_PERIOD=YYYY-MM`` and ends in ``.parquet``.
    """
    period = f"BILLING_PERIOD={day:%Y-%m}"
    keys: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if period in obj["Key"] and obj["Key"].endswith(".parquet"):
                keys.append(obj["Key"])
    return sorted(keys)


def ingest(event: dict[str, Any], cfg: Config, deps: Deps) -> dict[str, Any]:
    now = deps.now()
    target = _target_day(event, now)
    days = [target - timedelta(days=k) for k in range(REVISION_DAYS, -1, -1)]
    store = S3ParquetStore(deps.s3, cfg.bucket, cfg.normalized_prefix)
    loaded: dict[str, int] = {}
    with tempfile.TemporaryDirectory() as tmp:
        paths: list[str] = []
        for month_day in {d for d in days}:
            for key in list_cur_objects(deps.s3, cfg.bucket, cfg.cur_prefix, month_day):
                local = os.path.join(tmp, key.replace("/", "_"))
                if local not in paths:
                    deps.s3.download_file(cfg.bucket, key, local)
                    paths.append(local)
        if not paths:
            return {"day": target.isoformat(), "loaded": {}, "warning": "no CUR files found yet"}
        rows = read_cur_parquet(paths)
    for d in days:
        day_rows = [r for r in rows if r.day == d]
        if day_rows or d == target:
            store.load_day(d, day_rows)  # replaces that day: re-running never duplicates
            loaded[d.isoformat()] = len(day_rows)

    summary: dict[str, Any] = {"day": target.isoformat(), "loaded": loaded}
    if deps.ce is not None:
        totals = ce_module.fetch_daily_totals(deps.ce, days[0], target)
        for d, cents in totals.items():
            deps.s3.put_object(
                Bucket=cfg.bucket,
                Key=f"{cfg.raw_prefix}/ce/date={d.isoformat()}/total.json",
                Body=json.dumps({"day": d.isoformat(), "total_cents": cents}).encode(),
            )
        summary["ce_totals_saved"] = len(totals)
    return summary


# --------------------------------------------------------------------------- digest


def _read_ce_total(s3: Any, cfg: Config, day: date) -> int | None:
    try:
        obj = s3.get_object(
            Bucket=cfg.bucket, Key=f"{cfg.raw_prefix}/ce/date={day.isoformat()}/total.json"
        )
    except s3.exceptions.NoSuchKey:
        return None
    return int(json.loads(obj["Body"].read())["total_cents"])


def lookup_cloudtrail(client: Any, start: datetime, end: datetime) -> list[dict]:
    """cloudtrail:LookupEvents for the window (read-only), following pagination."""
    events: list[dict] = []
    token = None
    while True:
        kwargs: dict[str, Any] = {"StartTime": start, "EndTime": end, "MaxResults": 50}
        if token:
            kwargs["NextToken"] = token
        resp = client.lookup_events(**kwargs)
        events.extend(resp.get("Events", []))
        token = resp.get("NextToken")
        if not token:
            return events


def _secret(deps: Deps, secret_id: str) -> dict[str, str]:
    raw = deps.secrets.get_secret_value(SecretId=secret_id)["SecretString"]
    return json.loads(raw)


class S3StateSync:
    """Keeps the SQLite state file in S3: download before a run, upload after.

    Last write wins. That is fine for one scheduled digest plus occasional button clicks;
    for heavier use, move the state to DynamoDB or Postgres.
    """

    def __init__(self, s3: Any, bucket: str, key: str, local: str):
        self.s3, self.bucket, self.key, self.local = s3, bucket, key, local

    def pull(self) -> State:
        from botocore.exceptions import ClientError

        try:
            self.s3.download_file(self.bucket, self.key, self.local)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
                raise  # a real failure (permissions, network): do not silently start empty
            if os.path.exists(self.local):
                os.remove(self.local)  # first run: no state yet, start clean
        return State(self.local)

    def push(self, state: State) -> None:
        """Upload the state file and close the connection (the file is final for this run)."""
        state.db.commit()
        state.close()
        self.s3.upload_file(self.local, self.bucket, self.key)


def digest(
    event: dict[str, Any],
    cfg: Config,
    deps: Deps,
    state: State | None = None,
    slack_secret_id: str | None = None,
    llm_secret_id: str | None = None,
) -> dict[str, Any]:
    now = deps.now()
    day = _target_day(event, now)
    store = S3ParquetStore(deps.s3, cfg.bucket, cfg.normalized_prefix)
    start, end = history_window(day)
    rows = store.read_range(start, end)
    if not any(r.day == day for r in rows):
        return {"day": day.isoformat(), "sent": False, "warning": "no cost data for this day yet"}

    events = []
    if deps.cloudtrail is not None:
        window_start = datetime.combine(day - CLOUDTRAIL_PAD, datetime.min.time(), tzinfo=UTC)
        events = lookup_cloudtrail(
            deps.cloudtrail, window_start, window_start + 2 * CLOUDTRAIL_PAD + timedelta(days=1)
        )

    findings = []
    if cfg.waste and deps.session is not None:
        inventory = collect(deps.session)
        recent = [r for r in rows if r.day >= day - timedelta(days=7)]
        findings, _ = run_all(
            inventory,
            CloudWatchMetrics(deps.session.client("cloudwatch")),
            PriceBook(recent),
            WasteConfig(),
            now,
        )

    writer = deps.writer
    if writer is None and cfg.ai and deps.secrets is not None and llm_secret_id:
        os.environ["ANTHROPIC_API_KEY"] = _secret(deps, llm_secret_id)["api_key"]
        writer = AnthropicWriter()
    budget = TokenBudget(cfg.token_budget) if writer else None

    own_state = state is None
    sync = None
    if own_state:
        sync = S3StateSync(
            deps.s3,
            cfg.bucket,
            cfg.state_key,
            os.path.join(tempfile.gettempdir(), "costlens.sqlite"),
        )
        state = sync.pull()
    result = run_day(
        rows,
        day,
        cloudtrail=events,
        waste=findings,
        writer=writer,
        budget=budget,
        ce_total_cents=_read_ce_total(deps.s3, cfg, day),
        now=now,
        state=state,
        details_url=cfg.details_url,
    )

    sent = False
    if cfg.slack_channel and deps.secrets is not None and slack_secret_id:
        token = _secret(deps, slack_secret_id)["bot_token"]
        kwargs = {"http_post": deps.http_post} if deps.http_post else {}
        post_message(token, cfg.slack_channel, result.blocks, result.digest_text, **kwargs)
        sent = True
    if sync is not None:
        sync.push(state)
    return {
        "day": day.isoformat(),
        "sent": sent,
        "significant": result.report.significant,
        "is_alert": result.is_alert,
        "source": result.explanation.source,
        "findings": len(findings),
    }


# --------------------------------------------------------------------------- Lambda glue


def _live_deps() -> Deps:  # pragma: no cover - needs real AWS
    import boto3

    session = boto3.Session()
    return Deps(
        s3=session.client("s3"),
        ce=session.client("ce", region_name="us-east-1"),  # Cost Explorer is served from us-east-1
        cloudtrail=session.client("cloudtrail"),
        session=session,
        secrets=session.client("secretsmanager"),
    )


def lambda_ingest(event, context):  # pragma: no cover - thin wrapper
    return ingest(event or {}, Config.from_env(), _live_deps())


def lambda_digest(event, context):  # pragma: no cover - thin wrapper
    return digest(
        event or {},
        Config.from_env(),
        _live_deps(),
        slack_secret_id=os.environ.get("COSTLENS_SLACK_SECRET"),
        llm_secret_id=os.environ.get("COSTLENS_LLM_SECRET"),
    )
