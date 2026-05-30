"""Cost / token tracking from transcript JSONL (v0.3). Fully offline."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clauster import __main__ as cli
from clauster.usage import (
    PRICES,
    ModelPrice,
    TokenTotals,
    TranscriptUsage,
    cost_usd,
    parse_transcript,
)

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "transcripts" / "test1-session.jsonl"


def _transcript(tmp_path, records) -> Path:
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def _assistant(model, **usage):
    return {"type": "assistant", "message": {"role": "assistant", "model": model, "usage": usage}}


# ----- TokenTotals / cost ----------------------------------------------

def test_token_totals_accumulate():
    t = TokenTotals()
    t.add_usage({"input_tokens": 10, "output_tokens": 5,
                 "cache_creation_input_tokens": 100, "cache_read_input_tokens": 200})
    t.add_usage({"input_tokens": 1, "output_tokens": 2})
    assert (t.input, t.output, t.cache_creation, t.cache_read, t.messages) == (11, 7, 100, 200, 2)
    assert t.total_tokens == 11 + 7 + 100 + 200


def test_cost_usd_opus_exact():
    t = TokenTotals(input=6, output=13, cache_creation=11715, cache_read=17228)
    # 6*15 + 13*75 + 11715*18.75 + 17228*1.5, per Mtok
    expected = (6 * 15 + 13 * 75 + 11715 * 18.75 + 17228 * 1.5) / 1_000_000
    assert cost_usd("claude-opus-4-7", t) == pytest.approx(expected)


@pytest.mark.parametrize("model,family", [
    ("claude-opus-4-8", "opus"), ("claude-sonnet-4-6", "sonnet"), ("claude-haiku-4-5", "haiku"),
])
def test_cost_matches_family(model, family):
    t = TokenTotals(input=1_000_000)  # 1 Mtok input
    assert cost_usd(model, t) == pytest.approx(PRICES[family].input)


def test_cost_unknown_model_is_none():
    assert cost_usd("gpt-9", TokenTotals(input=100)) is None


def test_custom_prices():
    prices = {"opus": ModelPrice(input=1.0, output=1.0, cache_write=1.0, cache_read=1.0)}
    t = TokenTotals(input=1_000_000)
    assert cost_usd("claude-opus-4-8", t, prices) == pytest.approx(1.0)


# ----- parse_transcript ------------------------------------------------

def test_parse_fixture():
    u = parse_transcript(FIXTURE)
    assert "claude-opus-4-7" in u.by_model
    t = u.by_model["claude-opus-4-7"]
    assert (t.input, t.output, t.cache_creation, t.cache_read) == (6, 13, 11715, 17228)
    assert u.cost_usd() > 0


def test_parse_multi_model_and_totals(tmp_path):
    p = _transcript(tmp_path, [
        _assistant("claude-opus-4-8", input_tokens=10, output_tokens=20),
        _assistant("claude-sonnet-4-6", input_tokens=100, output_tokens=200),
        {"type": "user", "message": {"role": "user"}},  # no usage -> ignored
    ])
    u = parse_transcript(p)
    assert set(u.by_model) == {"claude-opus-4-8", "claude-sonnet-4-6"}
    assert u.totals.input == 110 and u.totals.output == 220 and u.totals.messages == 2


def test_parse_tolerates_blank_and_corrupt_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(
        json.dumps(_assistant("claude-opus-4-8", input_tokens=5)) + "\n"
        "\n"               # blank
        "{not json\n"      # corrupt
        + json.dumps({"type": "attachment"}) + "\n"  # no message
    )
    u = parse_transcript(p)
    assert u.totals.input == 5 and u.totals.messages == 1


def test_parse_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_transcript(tmp_path / "nope.jsonl")


def test_unpriced_models_reported(tmp_path):
    p = _transcript(tmp_path, [_assistant("gpt-9", input_tokens=1),
                               _assistant("claude-opus-4-8", input_tokens=1)])
    u = parse_transcript(p)
    assert u.unpriced_models() == ["gpt-9"]
    # unpriced model contributes 0 to the total, doesn't crash
    assert u.cost_usd() >= 0


# ----- CLI -------------------------------------------------------------

def test_cli_usage_fixture(capsys):
    assert cli.main(["usage", str(FIXTURE)]) == 0
    err = capsys.readouterr().err
    assert "claude-opus-4-7" in err and "tokens" in err


def test_cli_usage_missing_file(tmp_path):
    assert cli.main(["usage", str(tmp_path / "nope.jsonl")]) == 2
