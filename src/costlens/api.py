"""FastAPI service for the dashboard and Slack buttons.

Reads from the SQLite state. The only writes are Slack button clicks (verified with Slack's
request signature) and finding-status changes (behind a bearer token). Neither can touch AWS.
"""

from __future__ import annotations

import hmac
import json
import time
from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .slack import handle_interaction, parse_interaction_body, verify_signature
from .state import State

FINDING_STATUSES = ("open", "snoozed", "dismissed", "fixed")


class StatusBody(BaseModel):
    status: str


def create_app(
    state: State,
    *,
    slack_signing_secret: str | None = None,
    api_token: str | None = None,
    allowed_origins: list[str] | None = None,
    clock=time.time,
) -> FastAPI:
    app = FastAPI(title="CostLens API", version="0.1.0")
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type"],
        )

    def require_token(authorization: str | None = Header(default=None)) -> None:
        if not api_token:
            raise HTTPException(403, "write access is disabled: no API token is configured")
        supplied = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(supplied, api_token):
            raise HTTPException(401, "invalid token")

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "days": len(state.list_days(1000))}

    @app.get("/api/days")
    def days(limit: int = 90) -> list[dict]:
        return state.list_days(max(1, min(limit, 400)))

    @app.get("/api/trend")
    def trend(limit: int = 60) -> list[dict]:
        rows = state.list_days(max(1, min(limit, 400)))
        return [
            {"day": r["day"], "total_cents": r["total_cents"], "baseline_cents": r["baseline_cents"],
             "significant": r["significant"]}
            for r in reversed(rows)
        ]  # fmt: skip

    @app.get("/api/days/{day}")
    def one_day(day: str) -> dict:
        try:
            d = date.fromisoformat(day)
        except ValueError as exc:
            raise HTTPException(422, "day must be YYYY-MM-DD") from exc
        summary = next((r for r in state.list_days(1000) if r["day"] == day), None)
        exp = state.explanation_for(d)
        if summary is None or exp is None:
            raise HTTPException(404, "no report for that day")
        return {"summary": summary, "facts": exp["facts"], "explanation": exp["explanation"],
                "digest_text": exp["digest_text"]}  # fmt: skip

    @app.get("/api/explanations")
    def explanations(limit: int = 30) -> list[dict]:
        out = state.explanations(max(1, min(limit, 200)))
        return [
            {"day": e["day"], "source": e["source"], "facts_hash": e["facts_hash"],
             "headline": e["explanation"]["headline"], "digest_text": e["digest_text"]}
            for e in out
        ]  # fmt: skip

    @app.get("/api/waste")
    def waste(status: str | None = None) -> dict:
        if status is not None and status not in FINDING_STATUSES:
            raise HTTPException(422, f"status must be one of {FINDING_STATUSES}")
        return {"findings": state.findings(status), "savings": state.savings_summary()}

    @app.post("/api/waste/{rule}/{resource_id}/status", dependencies=[Depends(require_token)])
    def set_status(rule: str, resource_id: str, body: StatusBody) -> dict:
        if body.status not in FINDING_STATUSES:
            raise HTTPException(422, f"status must be one of {FINDING_STATUSES}")
        if not state.set_finding_status(rule, resource_id, body.status):
            raise HTTPException(404, "no such finding")
        return {"ok": True}

    @app.post("/slack/interactions")
    async def slack_interactions(request: Request) -> dict:
        body = await request.body()
        if not verify_signature(
            slack_signing_secret or "",
            request.headers.get("x-slack-request-timestamp"),
            body,
            request.headers.get("x-slack-signature"),
            now=clock(),
        ):
            raise HTTPException(401, "bad Slack signature")
        try:
            payload = parse_interaction_body(body)
        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(400, "malformed interaction payload") from exc
        return handle_interaction(state, payload)

    return app
