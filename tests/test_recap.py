"""Tests for the resume-recap SessionStart hook and its settings.json installer."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from clauster.hooks import resume_recap as hook
from clauster.recap import ensure_recap_hook_installed, hook_command


def _write_transcript(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


# ----- extract_turns --------------------------------------------------------


def test_extract_turns_keeps_user_assistant_skips_noise(tmp_path: Path) -> None:
    t = tmp_path / "s.jsonl"
    _write_transcript(
        t,
        [
            {"type": "queue-operation", "content": "noise", "operation": "x"},
            {"type": "attachment", "content": "blah"},
            {"type": "user", "message": {"content": "hello there"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hi back"}]}},
            # assistant with mixed blocks: only text survives
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash", "input": {}},
                        {"type": "text", "text": "ran a command"},
                    ]
                },
            },
            {"type": "user", "message": {"content": "   "}},  # empty -> skipped
        ],
    )
    turns = hook.extract_turns(str(t))
    assert turns == [
        ("user", "hello there"),
        ("assistant", "hi back"),
        ("assistant", "ran a command"),
    ]


def test_extract_turns_skips_prior_recap_to_avoid_compounding(tmp_path: Path) -> None:
    t = tmp_path / "s.jsonl"
    _write_transcript(
        t,
        [
            {"type": "user", "message": {"content": f"{hook.SENTINEL}\n# old recap\nstuff"}},
            {"type": "user", "message": {"content": "real message"}},
        ],
    )
    assert hook.extract_turns(str(t)) == [("user", "real message")]


def test_extract_turns_tolerates_malformed_lines(tmp_path: Path) -> None:
    t = tmp_path / "s.jsonl"
    t.write_text(
        "not json\n"
        + json.dumps({"type": "user", "message": {"content": "ok"}})
        + "\n{bad json\n",
        encoding="utf-8",
    )
    assert hook.extract_turns(str(t)) == [("user", "ok")]


def test_extract_turns_missing_file_returns_empty(tmp_path: Path) -> None:
    assert hook.extract_turns(str(tmp_path / "nope.jsonl")) == []


def test_text_from_content_ignores_non_text() -> None:
    assert hook._text_from_content(None) == ""
    assert hook._text_from_content(123) == ""
    assert hook._text_from_content([{"type": "tool_use", "name": "Bash"}]) == ""


# ----- find_prior_transcript ------------------------------------------------


def test_find_prior_picks_newest_excluding_current(tmp_path: Path) -> None:
    old = tmp_path / "old.jsonl"
    cur = tmp_path / "cur.jsonl"
    new = tmp_path / "new.jsonl"
    for p in (old, cur, new):
        p.write_text("{}\n")
    os.utime(old, (1000, 1000))
    os.utime(new, (3000, 3000))
    os.utime(cur, (4000, 4000))  # current is newest, must be excluded
    assert hook.find_prior_transcript(str(tmp_path), "cur") == str(new)


def test_find_prior_none_when_only_current(tmp_path: Path) -> None:
    (tmp_path / "cur.jsonl").write_text("{}\n")
    assert hook.find_prior_transcript(str(tmp_path), "cur") is None


def test_find_prior_none_when_empty_dir(tmp_path: Path) -> None:
    assert hook.find_prior_transcript(str(tmp_path), "cur") is None


# ----- build_recap ----------------------------------------------------------


def test_build_recap_empty_turns_is_empty() -> None:
    assert hook.build_recap([], 8000) == ""


def test_build_recap_has_sentinel_header_and_turns() -> None:
    recap = hook.build_recap([("user", "ping"), ("assistant", "pong")], 8000)
    assert recap.startswith(hook.SENTINEL)
    assert "**User:** ping" in recap
    assert "**Assistant:** pong" in recap
    assert "End of recap" in recap


def test_build_recap_trims_oldest_when_over_budget() -> None:
    turns = [("user", "AAAA" * 100), ("user", "ZZZZ" * 100)]
    recap = hook.build_recap(turns, max_chars=500)
    assert "ZZZZ" in recap  # most recent kept
    assert "AAAA" not in recap  # oldest trimmed
    assert "trimmed" in recap


# ----- compute_recap (end to end) -------------------------------------------


def test_compute_recap_recaps_prior_transcript(tmp_path: Path) -> None:
    prior = tmp_path / "prior.jsonl"
    cur = tmp_path / "cur.jsonl"
    _write_transcript(
        prior,
        [
            {"type": "user", "message": {"content": "Codeword is ECHO-1001."}},
            {"type": "assistant", "message": {"content": "Acknowledged."}},
        ],
    )
    cur.write_text("{}\n")
    os.utime(prior, (1000, 1000))
    os.utime(cur, (2000, 2000))
    recap = hook.compute_recap(str(cur), "cur", 8000)
    assert "ECHO-1001" in recap
    assert "Acknowledged" in recap


def test_compute_recap_empty_without_prior(tmp_path: Path) -> None:
    cur = tmp_path / "cur.jsonl"
    cur.write_text("{}\n")
    assert hook.compute_recap(str(cur), "cur", 8000) == ""


# ----- main() (the actual hook entrypoint) ----------------------------------


def _run_main(monkeypatch, payload: dict, env: dict[str, str]) -> str:
    for key in (hook.ENV_FLAG, hook.ENV_MAX_CHARS):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    hook.main()
    return out.getvalue()


@pytest.fixture
def prior_project(tmp_path: Path) -> tuple[Path, Path]:
    prior = tmp_path / "prior.jsonl"
    cur = tmp_path / "cur.jsonl"
    _write_transcript(prior, [{"type": "user", "message": {"content": "remember FOXTROT"}}])
    cur.write_text("{}\n")
    os.utime(prior, (1000, 1000))
    os.utime(cur, (2000, 2000))
    return prior, cur


def test_main_injects_additional_context_when_enabled(monkeypatch, prior_project) -> None:
    _prior, cur = prior_project
    out = _run_main(
        monkeypatch,
        {"transcript_path": str(cur), "session_id": "cur", "source": "startup"},
        {hook.ENV_FLAG: "1"},
    )
    payload = json.loads(out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "FOXTROT" in payload["hookSpecificOutput"]["additionalContext"]


def test_main_noop_without_env_flag(monkeypatch, prior_project) -> None:
    _prior, cur = prior_project
    out = _run_main(
        monkeypatch,
        {"transcript_path": str(cur), "session_id": "cur", "source": "startup"},
        {},  # flag absent
    )
    assert out == ""


def test_main_noop_on_compact_source(monkeypatch, prior_project) -> None:
    _prior, cur = prior_project
    out = _run_main(
        monkeypatch,
        {"transcript_path": str(cur), "session_id": "cur", "source": "compact"},
        {hook.ENV_FLAG: "1"},
    )
    assert out == ""


def test_main_noop_without_transcript_path(monkeypatch) -> None:
    out = _run_main(monkeypatch, {"session_id": "cur", "source": "startup"}, {hook.ENV_FLAG: "1"})
    assert out == ""


def test_main_noop_when_payload_not_dict(monkeypatch) -> None:
    out = _run_main(monkeypatch, [], {hook.ENV_FLAG: "1"})  # JSON list, not an object
    assert out == ""


def test_main_noop_when_no_prior_transcript(monkeypatch, tmp_path) -> None:
    cur = tmp_path / "cur.jsonl"
    cur.write_text("{}\n")
    out = _run_main(
        monkeypatch,
        {"transcript_path": str(cur), "session_id": "cur", "source": "startup"},
        {hook.ENV_FLAG: "1"},
    )
    assert out == ""  # nothing prior to recap


def test_main_tolerates_bad_stdin(monkeypatch) -> None:
    monkeypatch.setenv(hook.ENV_FLAG, "1")
    monkeypatch.setattr("sys.stdin", io.StringIO("}{ not json"))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    hook.main()
    assert out.getvalue() == ""


def test_max_chars_env_parsing(monkeypatch) -> None:
    monkeypatch.delenv(hook.ENV_MAX_CHARS, raising=False)
    assert hook._max_chars() == hook.DEFAULT_MAX_CHARS
    monkeypatch.setenv(hook.ENV_MAX_CHARS, "1234")
    assert hook._max_chars() == 1234
    monkeypatch.setenv(hook.ENV_MAX_CHARS, "10")  # below floor -> default
    assert hook._max_chars() == hook.DEFAULT_MAX_CHARS
    monkeypatch.setenv(hook.ENV_MAX_CHARS, "junk")
    assert hook._max_chars() == hook.DEFAULT_MAX_CHARS


# ----- ensure_recap_hook_installed ------------------------------------------


def test_installer_creates_and_is_idempotent(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    assert ensure_recap_hook_installed(settings) is True
    data = json.loads(settings.read_text())
    entries = data["hooks"]["SessionStart"]
    assert len(entries) == 1
    assert "resume_recap.py" in entries[0]["hooks"][0]["command"]
    # second call: already present -> no change, still one entry
    assert ensure_recap_hook_installed(settings) is False
    assert len(json.loads(settings.read_text())["hooks"]["SessionStart"]) == 1


def test_installer_preserves_existing_hooks(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    existing = {
        "hooks": {
            "SessionStart": [
                {"matcher": "", "hooks": [{"type": "command", "command": "other-tool"}]}
            ],
            "PostToolUse": [{"matcher": "Bash", "hooks": []}],
        },
        "model": "opus",
    }
    settings.write_text(json.dumps(existing))
    assert ensure_recap_hook_installed(settings) is True
    data = json.loads(settings.read_text())
    assert data["model"] == "opus"  # untouched
    assert "PostToolUse" in data["hooks"]  # untouched
    commands = [h["command"] for e in data["hooks"]["SessionStart"] for h in e["hooks"]]
    assert "other-tool" in commands  # preserved
    assert any("resume_recap.py" in c for c in commands)  # added


def test_installer_recovers_from_malformed_settings(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text("not valid json {{{")
    assert ensure_recap_hook_installed(settings) is True
    data = json.loads(settings.read_text())
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"]


def test_installer_updates_interpreter_in_place(tmp_path: Path) -> None:
    """A changed interpreter (moved venv) updates the same entry, not duplicates."""
    settings = tmp_path / "settings.json"
    script = Path("/opt/clauster/hooks/resume_recap.py")
    ensure_recap_hook_installed(settings, command=f'"/old/python" "{script}"', script=script)
    # re-run with a different interpreter but the same script path -> idempotent
    changed = ensure_recap_hook_installed(
        settings, command=f'"/new/python" "{script}"', script=script
    )
    assert changed is False
    assert len(json.loads(settings.read_text())["hooks"]["SessionStart"]) == 1


def test_installer_ignores_non_dict_session_start_entries(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"SessionStart": ["junk", 42]}}))
    assert ensure_recap_hook_installed(settings) is True
    commands = [
        h["command"]
        for e in json.loads(settings.read_text())["hooks"]["SessionStart"]
        if isinstance(e, dict)
        for h in e["hooks"]
    ]
    assert any("resume_recap.py" in c for c in commands)


def test_hook_command_quotes_interpreter_and_script() -> None:
    # Compare against the platform's own Path rendering (Windows uses backslashes).
    script = Path("/x/resume_recap.py")
    cmd = hook_command(python="/usr/bin/python3", script=script)
    assert cmd == f'"/usr/bin/python3" "{script}"'
