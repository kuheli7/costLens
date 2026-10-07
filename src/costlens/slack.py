"""Slack delivery: digest text, Block Kit message, signature check, button handling.

Everything here is a pure function except `post_message`, which takes an injectable HTTP
function so tests never touch the network.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from .explain import Explanation
from .state import State

SIGNATURE_TOLERANCE_SECONDS = 300  # Slack's own recommendation; blocks replayed requests
ACK_ACTION = "costlens_ack"
SNOOZE_ACTION = "costlens_snooze_24h"
SNOOZE_FOR = timedelta(hours=24)
MAX_SECTION_CHARS = 2900  # Slack rejects section text over 3000 characters


def escape(text: str) -> str:
    """Escape the three characters Slack treats as control characters in mrkdwn."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _top_line(facts: dict[str, Any]) -> str:
    if facts["significant_change"]:
        return (
            f"AWS spend {facts['day']}: {facts['total_spend']} "
            f"({facts['change']}, {facts['direction']} {facts['change_pct']} vs usual "
            f"{facts['typical_spend']})"
        )
    return (
        f"AWS spend {facts['day']}: {facts['total_spend']} "
        f"(in line with usual {facts['typical_spend']})"
    )


def _untagged_line(facts: dict[str, Any]) -> str | None:
    u = facts["untagged"]
    if u["amount"] == "$0.00":
        return None
    n = u["resource_count"]
    noun = "resource" if n == 1 else "resources"
    return f"Untagged spend: {u['amount']} ({u['share']}), {n} {noun} missing the team tag"


def _waste_lines(facts: dict[str, Any]) -> list[str]:
    w = facts["waste"]
    if not w["count"]:
        return []
    lines = [f"Waste found: {w['monthly_total']}/month across {w['count']} findings"]
    for item in w["top"]:
        lines.append(
            f"{item['summary']} (saves {item['monthly_saving']}/month, {item['saving_basis']})"
        )
    return lines


def alert_note(status: dict[str, Any] | None) -> str | None:
    """One line saying an alert is paused and by whom, or None when it is live."""
    if not status:
        return None
    if status["status"] == "snoozed" and status.get("until"):
        return (
            f"Alert snoozed by {status['by']} until {status['until'][:16].replace('T', ' ')} UTC."
        )
    return f"Alert acknowledged by {status['by']}; no repeat alerts today."


def render_digest(facts: dict[str, Any], exp: Explanation, note: str | None = None) -> str:
    """Plain-text digest. Figures come from the fact sheet; sentences from the explanation."""
    lines = [_top_line(facts), "", exp.headline, ""]
    by_id = {c["id"]: c for c in facts["causes"]}
    for n, c in enumerate(exp.causes, start=1):
        fact = by_id[c["id"]]
        lines.append(f"{n}. {fact['change']} - {c['explanation']}")
        lines.append(f"   Fix: {c['fix']}")
    if exp.causes:
        lines.append("")
    untagged = _untagged_line(facts)
    if untagged:
        lines.append(untagged)
    lines.extend(_waste_lines(facts))
    lines.append("")
    lines.append(exp.confidence_note)
    if note:
        lines.append(note)
    return "\n".join(lines).strip()


def build_blocks(
    facts: dict[str, Any],
    exp: Explanation,
    alert_key: str,
    details_url: str | None = None,
    note: str | None = None,
) -> list[dict[str, Any]]:
    """Slack Block Kit message.

    Acknowledge and Snooze buttons appear only while there is a live alert to act on: a
    significant change that nobody has acknowledged or snoozed. Once someone has, the message
    says so instead of offering the buttons again; on a quiet day there is nothing to act on.
    """

    def section(text: str) -> dict[str, Any]:
        return {"type": "section", "text": {"type": "mrkdwn", "text": text[:MAX_SECTION_CHARS]}}

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": _top_line(facts)[:150]}},
        section(escape(exp.headline)),
    ]
    by_id = {c["id"]: c for c in facts["causes"]}
    for n, c in enumerate(exp.causes, start=1):
        fact = by_id[c["id"]]
        blocks.append(
            section(
                f"*{n}. {escape(fact['change'])}* - {escape(c['explanation'])}\n"
                f"_Fix:_ {escape(c['fix'])}"
            )
        )
    extras = [x for x in [_untagged_line(facts), *_waste_lines(facts)] if x]
    if extras:
        blocks.append({"type": "divider"})
        blocks.append(section("\n".join(escape(x) for x in extras)))
    context = [exp.confidence_note, *facts["warnings"], *([note] if note else [])]
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": escape(t)[:300]} for t in context if t][:10],
        }
    )
    live_alert = facts["significant_change"] and not note
    buttons: list[dict[str, Any]] = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Acknowledge"},
            "action_id": ACK_ACTION,
            "value": alert_key,
            "style": "primary",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Snooze 24h"},
            "action_id": SNOOZE_ACTION,
            "value": alert_key,
        },
    ]
    if not live_alert:
        buttons = []
    if details_url:
        buttons.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "View details"},
                "url": details_url,
                "action_id": "costlens_details",
            }
        )
    if buttons:
        blocks.append({"type": "actions", "elements": buttons})
    return blocks


# --------------------------------------------------------------------------- security


def sign(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


def verify_signature(
    secret: str,
    timestamp: str | None,
    body: bytes,
    signature: str | None,
    now: float | None = None,
    tolerance: int = SIGNATURE_TOLERANCE_SECONDS,
) -> bool:
    """Verify Slack's X-Slack-Signature. Rejects bad, missing and stale (replayed) requests."""
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


def parse_interaction_body(body: bytes) -> dict[str, Any]:
    """Slack posts interactions as application/x-www-form-urlencoded with a `payload` JSON field."""
    form = urllib.parse.parse_qs(body.decode("utf-8"))
    return json.loads(form["payload"][0])


def handle_interaction(state: State, payload: dict[str, Any], now: datetime | None = None) -> dict:
    """Apply an Acknowledge / Snooze click and return Slack's response body."""
    now = now or datetime.now(UTC)
    actions = payload.get("actions") or []
    if not actions:
        return {"response_type": "ephemeral", "text": "Nothing to do."}
    action = actions[0]
    key = action.get("value", "")
    user = (payload.get("user") or {}).get("username") or (payload.get("user") or {}).get("id", "?")
    if action.get("action_id") == ACK_ACTION:
        state.set_alert(key, "acked", user, now)
        text = f"Acknowledged by {user}. No more alerts for this today."
    elif action.get("action_id") == SNOOZE_ACTION:
        state.set_alert(key, "snoozed", user, now, SNOOZE_FOR)
        text = f"Snoozed by {user} for 24 hours."
    else:
        return {"response_type": "ephemeral", "text": "Unknown action."}
    return {"response_type": "ephemeral", "replace_original": False, "text": text}


# --------------------------------------------------------------------------- sending


HttpPost = Callable[[str, bytes, dict[str, str]], dict[str, Any]]


def _urllib_post(url: str, data: bytes, headers: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - fixed https URL
        return json.loads(resp.read().decode("utf-8"))


def post_message(
    token: str,
    channel: str,
    blocks: list[dict[str, Any]],
    fallback_text: str,
    http_post: HttpPost = _urllib_post,
) -> dict[str, Any]:
    """chat.postMessage. Raises RuntimeError if Slack answers ok=false."""
    body = json.dumps({"channel": channel, "blocks": blocks, "text": fallback_text}).encode()
    result = http_post(
        "https://slack.com/api/chat.postMessage",
        body,
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
    )
    if not result.get("ok"):
        raise RuntimeError(f"Slack rejected the message: {result.get('error', 'unknown error')}")
    return result
