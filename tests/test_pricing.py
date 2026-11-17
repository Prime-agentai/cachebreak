from __future__ import annotations

import json
from pathlib import Path

import pytest

from cachebreak.pricing import FALLBACK, TABLE, PriceBook, model_key
from cachebreak.transcript import Usage


@pytest.mark.parametrize("model,key", [
    ("claude-opus-5-5", "opus-5.5"),
    ("claude-opus-4-5-20251101", "opus-4.5"),
    ("claude-sonnet-4-20250514", "sonnet-4"),
    ("claude-3-7-sonnet-20250219", "sonnet-3.7"),  # legacy naming puts the version first
    ("claude-3-5-haiku-20241022", "haiku-3.5"),
    ("claude-sonnet-20250219", None),  # a date is not a version
    ("claude-haiku-4-5", "haiku-4.5"),
    ("us.anthropic.claude-sonnet-4-5-20250929-v1:0", "sonnet-4.5"),
    ("claude-fable-5-2", "fable-5.2"),
    ("claude-fable-5", "fable-5"),
    ("gpt-5", None),
    (None, None),
])
def test_model_key(model: str | None, key: str | None) -> None:
    assert model_key(model) == key


def test_published_multipliers() -> None:
    # Spot-check rows against the 2026-11-17 price page.
    o = TABLE["opus-5.5"]
    assert (o.input, o.write_5m, o.write_1h, o.read, o.output) == (4, 5, 8, 0.2, 20)
    f = TABLE["fable-5.2"]
    assert (f.input, f.write_1h, f.read, f.output) == (8, 16, 0.2, 40)
    s = TABLE["sonnet-5"]
    assert (s.read, s.output) == (pytest.approx(0.2), 10)


def test_haiku_55_long_prompt_tier() -> None:
    book = PriceBook()
    assert book.rates("claude-haiku-5-5", 50_000).input == pytest.approx(0.10)
    assert book.rates("claude-haiku-5-5", 150_000).input == pytest.approx(0.50)


def test_unknown_model_falls_back_and_is_flagged() -> None:
    book = PriceBook()
    assert book.rates("some-new-model") is FALLBACK
    assert book.unpriced == {"some-new-model"}


def test_cost_and_blended_write_rate() -> None:
    r = TABLE["opus-5.5"]
    u = Usage(input=1_000_000, write_5m=500_000, write_1h=500_000, read=1_000_000, output=0)
    assert r.cost(u) == pytest.approx(4 + 2.5 + 4 + 0.2)
    assert r.write_rate(u) == pytest.approx(6.5)
    assert r.write_rate(Usage()) == r.write_5m


def test_price_overrides(tmp_path: Path) -> None:
    f = tmp_path / "prices.json"
    f.write_text(json.dumps({"claude-opus-5-5": {"input": 1}, "acme-1": {"input": 2, "read": 0}}))
    book = PriceBook.from_file(str(f))
    assert book.rates("claude-opus-5-5").write_1h == 2
    assert book.rates("acme-1").read == 0
    assert book.unpriced == set()
    f.write_text("[]")
    with pytest.raises(ValueError):
        PriceBook.from_file(str(f))
