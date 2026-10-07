"""Reject generated text that states anything not in the fact sheet.

The checks are deliberately strict: a false rejection costs one retry and then a plainer
template sentence, while a missed invention is a wrong number in front of an engineer.

Checked: every number (allowing rounding), every identifier (resource IDs, ARNs, usage
types, group names), times and dates, number words, and the name after "by".
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

# identifier-like tokens: contain a digit, hyphen, colon or slash inside a word
_IDENT = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:/@-]*[A-Za-z0-9_]")
_TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD = re.compile(r"[A-Za-z][A-Za-z']*")
_BY = re.compile(r"\bby\s+([A-Za-z0-9_.@-]+)", re.IGNORECASE)

# Ordinary hyphenated or digit-bearing words that are not identifiers.
SAFE_TOKENS = frozenset(
    {
        "ec2", "s3", "rds", "ebs", "nat", "asg", "ssd", "gb", "mb", "tb", "ap-south-1",
        "scale-out", "scale-in", "day-over-day", "same-weekday", "time-only", "follow-up",
        "up-to-date", "long-running", "per-hour", "per-day", "cost-saving", "open-source",
        "month-end", "well-known", "re-run", "e-mail", "on-demand", "one-off", "double-check",
        "pay-as-you-go", "first-time", "so-called", "last-minute",
    }
)  # fmt: skip

NUMBER_WORDS = {
    "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "twenty": 20, "thirty": 30, "fifty": 50,
    "hundred": 100, "thousand": 1000, "million": 1_000_000,
    "double": 2, "doubled": 2, "twice": 2, "triple": 3, "tripled": 3, "half": 0.5,
    "quadruple": 4, "quadrupled": 4,
}  # fmt: skip

STOPWORDS_AFTER_BY = frozenset(
    "the a an about around roughly nearly almost far more less this that these those its "
    "their our your his her one some any each every up down over under between at "
    "default now then just only also still much many most".split()
)


def _to_decimal(s: str) -> Decimal | None:
    try:
        return Decimal(s.replace(",", ""))
    except Exception:
        return None


def _strings(obj: Any):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


class Allowed:
    """Everything a piece of text may mention, derived from a fact sheet."""

    def __init__(self, facts: dict[str, Any]):
        self.idents: set[str] = set()
        self.times: set[str] = set()
        self.dates: set[str] = set()
        self.numbers: list[Decimal] = []
        self.words: set[str] = set()
        for text in _strings(facts):
            remaining = text
            for m in _ISO_DATE.findall(remaining):
                self.dates.add(m)
            remaining = _ISO_DATE.sub(" ", remaining)
            for m in _TIME.findall(remaining):
                self.times.add(m)
            remaining = _TIME.sub(" ", remaining)
            for tok in _IDENT.findall(remaining):
                if _is_identifier(tok):
                    self.idents.add(tok.lower())
                    remaining = remaining.replace(tok, " ")
            for n in _NUMBER.findall(remaining):
                d = _to_decimal(n)
                if d is not None:
                    self.numbers.append(d)
            self.words.update(w.lower() for w in _WORD.findall(text))

    def number_ok(self, text_number: str) -> bool:
        n = _to_decimal(text_number)
        if n is None:
            return True
        decimals = len(text_number.split(".")[1]) if "." in text_number else 0
        q = Decimal(1).scaleb(-decimals)
        for v in self.numbers:
            if v.quantize(q, rounding=ROUND_HALF_UP) == n:
                return True
            # a figure quoted without its trailing zeros ("12" for "12.00")
            if decimals == 0 and v == n:
                return True
        return False


def _is_identifier(tok: str) -> bool:
    """Starts with a letter and carries a digit or separator: i-0abc, api-prod-asg, m5.large."""
    return tok[0].isalpha() and any(ch.isdigit() or ch in "-:/@" for ch in tok)


def validate_text(text: str, allowed: Allowed) -> list[str]:
    """Return a list of problems; empty means the text only states facts from the sheet."""
    problems: list[str] = []
    remaining = text

    for d in _ISO_DATE.findall(remaining):
        if d not in allowed.dates:
            problems.append(f"date not in facts: {d}")
    remaining = _ISO_DATE.sub(" ", remaining)

    for t in _TIME.findall(remaining):
        if t not in allowed.times:
            problems.append(f"time not in facts: {t}")
    remaining = _TIME.sub(" ", remaining)

    for tok in _IDENT.findall(remaining):
        if not _is_identifier(tok):
            continue
        low = tok.lower().rstrip(".:")
        if low in allowed.idents or low in SAFE_TOKENS or tok.lower() in allowed.idents:
            remaining = remaining.replace(tok, " ")
            continue
        # a token that is only digits and separators is a number, handled below
        if re.fullmatch(r"[\d.,:/-]+", tok):
            continue
        problems.append(f"identifier not in facts: {tok}")
        remaining = remaining.replace(tok, " ")

    for n in _NUMBER.findall(remaining):
        if not allowed.number_ok(n):
            problems.append(f"number not in facts: {n}")

    for w in _WORD.findall(text):
        value = NUMBER_WORDS.get(w.lower())
        if value is None:
            continue
        if not any(v == Decimal(str(value)) for v in allowed.numbers):
            problems.append(f"number word not in facts: {w}")

    for m in _BY.finditer(text):
        who = m.group(1).rstrip(".,;:").lower()
        if who in STOPWORDS_AFTER_BY or re.fullmatch(r"[$\d.,%+-]+", who):
            continue
        if who not in allowed.words and who not in allowed.idents:
            problems.append(f"name after 'by' not in facts: {m.group(1)}")

    return problems
