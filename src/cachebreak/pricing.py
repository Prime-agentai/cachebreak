"""API list prices per model, used to turn token counts into dollars.

Source: https://platform.claude.com/docs/en/about-claude/pricing (read 2026-11-17).
These are API-equivalent prices. On a Claude subscription you do not pay them per
token, but they are the best available proxy for how fast a session drains your
usage limits. Override or extend the table with ``--prices FILE.json``:

    {"claude-opus-5-5": {"input": 4, "write_5m": 5, "write_1h": 8, "read": 0.2, "output": 20}}
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .transcript import Usage

PRICES_AS_OF = "2026-11-17"


@dataclass(frozen=True)
class Rates:
    """US dollars per million tokens."""

    input: float
    write_5m: float
    write_1h: float
    read: float
    output: float

    def cost(self, u: Usage) -> float:
        return (u.input * self.input + u.write_5m * self.write_5m + u.write_1h * self.write_1h
                + u.read * self.read + u.output * self.output) / 1_000_000

    def write_rate(self, u: Usage) -> float:
        """Blended write price for this request's split of 5m/1h writes."""
        if u.write == 0:
            return self.write_5m
        return (u.write_5m * self.write_5m + u.write_1h * self.write_1h) / u.write


def _row(inp: float, read_mult: float = 0.1, out_mult: float = 5.0) -> Rates:
    return Rates(inp, inp * 1.25, inp * 2.0, inp * read_mult, inp * out_mult)


# Keyed by "<family>-<major>[.<minor>]".
TABLE: dict[str, Rates] = {
    "fable-5.2": _row(8, 0.025), "mythos-5.2": _row(8, 0.025),
    "fable-5.1": _row(10, 0.025), "mythos-5.1": _row(10, 0.025),
    "fable-5": _row(10), "mythos-5": _row(10),
    "opus-5.5": _row(4, 0.05), "opus-5": _row(5), "opus-4.8": _row(5), "opus-4.7": _row(5),
    "opus-4.6": _row(5), "opus-4.5": _row(5), "opus-4.1": _row(15), "opus-4": _row(15),
    "sonnet-5.5": _row(2, 0.05), "sonnet-5": _row(2), "sonnet-4.6": _row(3),
    "sonnet-4.5": _row(3), "sonnet-4": _row(3), "sonnet-3.7": _row(3),
    "haiku-5.5": _row(0.10), "haiku-4.5": _row(1), "haiku-3.5": _row(0.8),
}
# Haiku 5.5 is priced higher for prompts over 100k tokens.
TIERS: dict[str, tuple[int, Rates]] = {"haiku-5.5": (100_000, _row(0.50))}

# Unknown models: standard multipliers on a $3/MTok base, flagged in the report.
FALLBACK = _row(3)

_MODEL_RE = re.compile(r"(fable|mythos|opus|sonnet|haiku)-(\d{1,2})(?:[-.](\d{1,2}))?(?!\d)")
# Legacy ids put the version first: claude-3-7-sonnet-20250219, claude-3-5-haiku-20241022.
_LEGACY_RE = re.compile(r"claude-(\d)(?:-(\d))?-(opus|sonnet|haiku)")


def model_key(model: str | None) -> str | None:
    """'claude-opus-4-5-20251101' -> 'opus-4.5'; 'claude-sonnet-4-20250514' -> 'sonnet-4'."""
    if not model:
        return None
    low = model.lower()
    legacy = _LEGACY_RE.search(low)
    if legacy:
        major, minor, family = legacy.groups()
    else:
        m = _MODEL_RE.search(low)
        if not m:
            return None
        family, major, minor = m.groups()
    return f"{family}-{major}.{minor}" if minor else f"{family}-{major}"


class PriceBook:
    def __init__(self, overrides: dict[str, Rates] | None = None) -> None:
        self.overrides = overrides or {}
        self.unpriced: set[str] = set()

    def rates(self, model: str | None, prompt_tokens: int = 0) -> Rates:
        if model and model in self.overrides:
            return self.overrides[model]
        key = model_key(model)
        if key and key in self.overrides:
            return self.overrides[key]
        if key in TIERS:
            threshold, high = TIERS[key]
            if prompt_tokens > threshold:
                return high
        if key and key in TABLE:
            return TABLE[key]
        self.unpriced.add(model or "unknown")
        return FALLBACK

    @classmethod
    def from_file(cls, path: str | None) -> PriceBook:
        if not path:
            return cls()
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("--prices file must be a JSON object of model -> rates")
        out: dict[str, Rates] = {}
        for name, row in raw.items():
            if not isinstance(row, dict) or "input" not in row:
                raise ValueError(f"--prices: {name!r} needs at least an 'input' price")
            base = float(row["input"])
            out[str(name)] = Rates(
                base,
                float(row.get("write_5m", base * 1.25)),
                float(row.get("write_1h", base * 2.0)),
                float(row.get("read", base * 0.1)),
                float(row.get("output", base * 5.0)),
            )
        return cls(out)
