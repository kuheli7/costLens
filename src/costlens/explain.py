"""Plain-English explanation layer: the model is a writer, never an analyst.

Flow: fact sheet in -> Writer produces three kinds of sentence -> validator checks every
number, identifier and name against the fact sheet -> one retry with the violations ->
deterministic template text if that fails too (or if the API is down, the budget is spent,
or AI is switched off). The final text is stored with its fact sheet so any sentence can be
traced back to the data.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from .factsheet import facts_hash
from .validate import Allowed, validate_text

PROMPT_VERSION = "v1"
MAX_HEADLINE_CHARS = 280
MAX_SENTENCE_CHARS = 420

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "causes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "explanation": {"type": "string"},
                    "fix": {"type": "string"},
                },
                "required": ["id", "explanation", "fix"],
                "additionalProperties": False,
            },
        },
        "confidence_note": {"type": "string"},
    },
    "required": ["headline", "causes", "confidence_note"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You write the plain-English part of a daily AWS cost digest for an engineer.

You receive a JSON fact sheet. Every figure in it is already computed and checked. You do not
analyse, calculate or infer; you only put the facts into clear sentences.

Output JSON with:
- headline: one or two sentences saying whether spend went up or down and by how much.
- causes: one entry per cause in the fact sheet, in the same order, each with the cause id,
  an explanation (what changed, and who or what did it if the context says so) and a fix
  (rephrase the fix_hint).
- confidence_note: one sentence on how sure we are. If a cause's context says the cause is not
  confirmed, say that plainly. If there are warnings, mention them briefly.

Rules, all strictly enforced by an automatic checker:
- Use only numbers, amounts, percentages, dates, times, resource names and person names that
  appear in the fact sheet, written the way the fact sheet writes them.
- Never add a number of your own: no counts, no ratios, no "twice", no "a few hundred".
- Never name a resource, group or person that is not in the fact sheet.
- Do not guess a cause. Do not suggest anything beyond the fix_hint.
- Keep each sentence short and free of jargon."""


class WriterError(Exception):
    """The writer could not produce text (API down, refusal, malformed reply)."""


class Writer(Protocol):
    prompt_version: str

    def write(self, facts: dict[str, Any], feedback: str | None = None) -> str:
        """Return a JSON string matching OUTPUT_SCHEMA. May raise WriterError."""
        ...


@dataclass(slots=True)
class TokenBudget:
    """Monthly token budget with a hard stop: once spent, the template writer takes over."""

    limit: int
    used: int = 0

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def charge(self, tokens: int) -> None:
        self.used += max(0, tokens)


@dataclass(slots=True)
class Explanation:
    headline: str
    causes: list[dict[str, str]]
    confidence_note: str
    source: str  # "llm" | "template"
    facts_hash: str
    attempts: int = 0
    rejected: list[list[str]] = field(default_factory=list)  # violations from rejected attempts
    cached: bool = False
    fallback_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "causes": self.causes,
            "confidence_note": self.confidence_note,
            "source": self.source,
            "facts_hash": self.facts_hash,
        }


# --------------------------------------------------------------------------- template


def _cause_sentence(c: dict[str, Any]) -> str:
    verb = {"up": "added", "down": "saved", "flat": "changed"}[c["direction"]]
    amount = c["change"].lstrip("+-")
    text = f"{c['what']} {verb} {amount} ({c['share_of_total_change']} of the change)."
    ctx = c["context"]
    if "event" in ctx:
        text += f" {ctx['event']} by {ctx['actor']} at {ctx['time_utc']} UTC ({ctx['confidence']})."
    else:
        text += " Cause not confirmed: no matching CloudTrail event."
    if "volume_effect" in c and c["rate_effect"] not in ("$0.00", "+$0.00"):
        text += f" Usage effect {c['volume_effect']}, price effect {c['rate_effect']}."
    return text


def template_text(facts: dict[str, Any]) -> dict[str, Any]:
    """Deterministic text from the fact sheet alone. Always passes the validator."""
    if facts["significant_change"]:
        headline = (
            f"Spend on {facts['day']} was {facts['total_spend']}, {facts['direction']} "
            f"{facts['change'].lstrip('+-')} ({facts['change_pct']}) from the usual "
            f"{facts['typical_spend']}."
        )
    else:
        headline = (
            f"Spend on {facts['day']} was {facts['total_spend']}, in line with the usual "
            f"{facts['typical_spend']}. No significant change."
        )
    causes = [
        {"id": c["id"], "explanation": _cause_sentence(c), "fix": c["fix_hint"].capitalize() + "."}
        for c in facts["causes"]
    ]
    unconfirmed = any("event" not in c["context"] for c in facts["causes"])
    note = "All causes matched a CloudTrail event." if facts["causes"] else ""
    if unconfirmed:
        note = "At least one cause is not confirmed by a CloudTrail event."
    if facts["warnings"]:
        note = (note + " Data notes: " + "; ".join(facts["warnings"]) + ".").strip()
    return {"headline": headline, "causes": causes, "confidence_note": note or "No caveats."}


class TemplateWriter:
    """A Writer that never calls a model. Used when AI is off or as the final fallback."""

    prompt_version = "template"

    def write(self, facts: dict[str, Any], feedback: str | None = None) -> str:
        return json.dumps(template_text(facts))


# --------------------------------------------------------------------------- validation


def check_output(raw: str, facts: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Parse writer output and validate structure and every sentence. Returns (data, problems)."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None, ["output is not valid JSON"]
    if not isinstance(data, dict):
        return None, ["output is not a JSON object"]
    problems: list[str] = []
    for key in ("headline", "causes", "confidence_note"):
        if key not in data:
            problems.append(f"missing field: {key}")
    if problems:
        return None, problems
    if not isinstance(data["headline"], str) or not isinstance(data["confidence_note"], str):
        return None, ["headline and confidence_note must be strings"]
    if not isinstance(data["causes"], list):
        return None, ["causes must be a list"]

    expected = [c["id"] for c in facts["causes"]]
    got = [c.get("id") if isinstance(c, dict) else None for c in data["causes"]]
    if got != expected:
        problems.append(f"cause ids {got} do not match the fact sheet {expected}")
    if len(data["headline"]) > MAX_HEADLINE_CHARS:
        problems.append("headline too long")

    allowed = Allowed(facts)
    texts = [("headline", data["headline"]), ("confidence_note", data["confidence_note"])]
    for c in data["causes"]:
        if not isinstance(c, dict):
            problems.append("a cause entry is not an object")
            continue
        for part in ("explanation", "fix"):
            value = c.get(part)
            if not isinstance(value, str):
                problems.append(f"cause {c.get('id')}: {part} must be a string")
                continue
            if len(value) > MAX_SENTENCE_CHARS:
                problems.append(f"cause {c.get('id')}: {part} too long")
            texts.append((f"{c.get('id')}.{part}", value))
    for where, text in texts:
        problems.extend(f"{where}: {p}" for p in validate_text(text, allowed))
    return (data if not problems else None), problems


# --------------------------------------------------------------------------- pipeline


def explain(
    facts: dict[str, Any],
    writer: Writer | None = None,
    *,
    cache: dict[str, Explanation] | None = None,
    budget: TokenBudget | None = None,
    retries: int = 1,
) -> Explanation:
    """Produce a validated explanation, falling back to template text when needed."""
    use_llm = writer is not None and not isinstance(writer, TemplateWriter)
    key = facts_hash(
        facts, getattr(writer, "prompt_version", "template") if use_llm else "template"
    )
    if cache is not None and key in cache:
        hit = cache[key]
        return Explanation(
            hit.headline,
            hit.causes,
            hit.confidence_note,
            hit.source,
            hit.facts_hash,
            hit.attempts,
            hit.rejected,
            True,
            hit.fallback_reason,
        )

    rejected: list[list[str]] = []
    attempts = 0
    reason: str | None = None
    result: Explanation | None = None

    if not use_llm:
        reason = "AI is off"
    elif budget is not None and budget.exhausted:
        reason = "monthly token budget spent"
    else:
        feedback: str | None = None
        for _ in range(1 + max(0, retries)):
            attempts += 1
            try:
                raw = writer.write(facts, feedback)
            except WriterError as exc:
                reason = f"writer error: {exc}"
                break
            usage = getattr(writer, "last_usage", 0)
            if budget is not None and usage:
                budget.charge(usage)
            data, problems = check_output(raw, facts)
            if data is not None:
                result = Explanation(
                    data["headline"],
                    data["causes"],
                    data["confidence_note"],
                    "llm",
                    key,
                    attempts,
                    rejected,
                )
                break
            rejected.append(problems)
            feedback = "Your previous answer was rejected: " + "; ".join(problems[:6])
            if budget is not None and budget.exhausted:
                break
        if result is None and reason is None:
            reason = "output failed validation"

    if result is None:
        data = template_text(facts)
        result = Explanation(
            data["headline"],
            data["causes"],
            data["confidence_note"],
            "template",
            key,
            attempts,
            rejected,
            False,
            reason,
        )
    if cache is not None:
        cache[key] = result
    return result


# --------------------------------------------------------------------------- test double


class FakeWriter:
    """Scripted writer for tests: returns queued replies, or a faithful template by default."""

    prompt_version = "fake"

    def __init__(self, replies: list[str] | None = None, usage: int = 0):
        self.replies = list(replies or [])
        self.calls: list[tuple[dict[str, Any], str | None]] = []
        self.last_usage = 0
        self._usage = usage

    def write(self, facts: dict[str, Any], feedback: str | None = None) -> str:
        self.calls.append((facts, feedback))
        self.last_usage = self._usage
        if self.replies:
            return self.replies.pop(0)
        return json.dumps(template_text(facts))


# --------------------------------------------------------------------------- Claude


class AnthropicWriter:
    """Writer backed by the Claude API. Needs the `llm` extra and credentials in the environment.

    The model ID comes from ``COSTLENS_MODEL`` (default ``claude-opus-5-5``). The call is one
    short request per digest, so the digest cost stays at cents per month; thinking depth is
    set to ``low`` because the task is rewriting, not reasoning.
    """

    prompt_version = PROMPT_VERSION

    def __init__(self, client: Any = None, model: str | None = None, max_tokens: int = 2000):
        if client is None:
            import anthropic  # imported lazily: the llm extra is optional

            client = anthropic.Anthropic()
        self._client = client
        self.model = model or os.environ.get("COSTLENS_MODEL", "claude-opus-5-5")
        self.max_tokens = max_tokens
        self.last_usage = 0

    def write(self, facts: dict[str, Any], feedback: str | None = None) -> str:
        import anthropic

        content = "Fact sheet:\n" + json.dumps(facts, indent=2)
        if feedback:
            content += "\n\n" + feedback + "\nWrite it again, fixing exactly those problems."
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=SYSTEM_PROMPT,
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
                },
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIError as exc:  # status, connection and timeout errors alike
            raise WriterError(f"{type(exc).__name__}: {exc}") from exc
        usage = getattr(response, "usage", None)
        self.last_usage = (
            (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "output_tokens", 0) or 0)
            if usage
            else 0
        )
        if response.stop_reason == "refusal":
            raise WriterError("the model declined to answer")
        if response.stop_reason == "max_tokens":
            raise WriterError("the reply was cut off")
        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            raise WriterError("empty reply")
        return text
