"""Tests for the config-deprecation registry + ``clauster config reconcile`` (#569).

Covers the pure decision logic (scan / build_plan / transforms), the config-writer
removal path, and the CLI end-to-end (dry-run, --yes, interactive). HOME is isolated
session-wide by conftest, so these never touch the real ~/.claude.json.
"""

from __future__ import annotations

import io

import pytest

from clauster import __main__ as cli
from clauster.config import load_config
from clauster.config_writer import write_edits
from clauster.reconcile import (
    DEPRECATIONS,
    Decision,
    apply_plan,
    build_plan,
    resume_mode_to_launch_mode,
    scan_config_file,
    scan_raw,
    show_cost_to_mode,
)

# ----- value transforms (THE single source of truth) --------------------------------


def test_resume_mode_transform_is_identity() -> None:
    assert resume_mode_to_launch_mode("pty") == "pty"
    assert resume_mode_to_launch_mode("standard") == "standard"


def test_show_cost_transform_false_maps_to_off_true_drops() -> None:
    assert show_cost_to_mode(False) == "off"
    # True was the historical default (badge shown) — no replacement value.
    assert show_cost_to_mode(True) is None


def test_config_validator_reuses_the_registry_transform(write_config) -> None:
    # Regression for "single source of truth": the load-time show_cost=false alias must
    # produce the same value the registry transform yields (config.py imports it).
    path = write_config("usage:\n  show_cost: false\n")
    assert load_config(path).usage.mode == show_cost_to_mode(False)
    # And the resume_mode alias carries the value the identity transform yields.
    path2 = write_config("claude:\n  resume_mode: pty\n")
    assert load_config(path2).claude.launch_mode == resume_mode_to_launch_mode("pty")


# ----- scanning ----------------------------------------------------------------------


def test_scan_raw_finds_both_deprecated_keys() -> None:
    raw = {"claude": {"resume_mode": "pty"}, "usage": {"show_cost": False}}
    findings = scan_raw(raw)
    keys = {f.deprecation.deprecated_key for f in findings}
    assert keys == {"claude.resume_mode", "usage.show_cost"}


def test_scan_raw_clean_config_is_empty() -> None:
    assert scan_raw({"claude": {"launch_mode": "pty"}, "usage": {"mode": "off"}}) == []


def test_scan_raw_proposes_mapped_value() -> None:
    (finding,) = scan_raw({"usage": {"show_cost": False}})
    assert finding.proposed_value == "off"
    assert finding.has_replacement is True
    assert finding.replacement_present is False


def test_scan_raw_drop_only_when_transform_returns_none() -> None:
    (finding,) = scan_raw({"usage": {"show_cost": True}})
    assert finding.has_replacement is False  # True -> no replacement value


def test_scan_raw_marks_replacement_present_when_both_set() -> None:
    (finding,) = scan_raw({"claude": {"resume_mode": "pty", "launch_mode": "standard"}})
    assert finding.replacement_present is True
    assert finding.has_replacement is False  # existing launch_mode already wins


def test_scan_config_file_non_mapping_root_is_empty(tmp_path) -> None:
    bad = tmp_path / "c.yml"
    bad.write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert scan_config_file(str(bad)) == []


# ----- build_plan (injected decide) --------------------------------------------------


def _accept_all(finding):
    return Decision(apply=True, value=finding.proposed_value, has_value=finding.has_replacement)


def test_build_plan_accept_all_removes_and_edits() -> None:
    findings = scan_raw({"claude": {"resume_mode": "pty"}, "usage": {"show_cost": False}})
    plan = build_plan(findings, _accept_all)
    assert set(plan.removals) == {"claude.resume_mode", "usage.show_cost"}
    assert plan.edits == {"claude.launch_mode": "pty", "usage.mode": "off"}
    assert plan.is_empty is False


def test_build_plan_skip_leaves_key_in_place() -> None:
    findings = scan_raw({"usage": {"show_cost": False}})
    plan = build_plan(findings, lambda f: Decision(apply=False))
    assert plan.removals == []
    assert plan.edits == {}
    assert plan.skipped[0].deprecation.deprecated_key == "usage.show_cost"
    assert plan.is_empty is True


def test_build_plan_drop_only_removes_without_edit() -> None:
    findings = scan_raw({"usage": {"show_cost": True}})
    plan = build_plan(findings, _accept_all)
    assert plan.removals == ["usage.show_cost"]
    assert plan.edits == {}  # no replacement value for show_cost: true


def test_build_plan_operator_picks_a_value() -> None:
    findings = scan_raw({"usage": {"show_cost": False}})
    plan = build_plan(findings, lambda f: Decision(apply=True, value="tokens", has_value=True))
    assert plan.edits == {"usage.mode": "tokens"}


def test_registry_covers_the_documented_keys() -> None:
    keys = {d.deprecated_key for d in DEPRECATIONS}
    assert {"claude.resume_mode", "usage.show_cost"} <= keys


# ----- config_writer removal path ----------------------------------------------------


def test_write_edits_removes_key_and_writes_replacement(write_config) -> None:
    path = write_config("claude:\n  resume_mode: pty  # comment\n")
    write_edits(path, {"claude.launch_mode": "pty"}, removals=["claude.resume_mode"])
    text = path.read_text(encoding="utf-8")
    assert "resume_mode" not in text
    assert "launch_mode: pty" in text
    reloaded = load_config(path)
    assert reloaded.claude.launch_mode == "pty"
    assert list(path.parent.glob(path.name + ".bak-*")), "expected a backup"


def test_write_edits_removal_only_no_edit(write_config) -> None:
    path = write_config("usage:\n  show_cost: true\n  fx_rate: 2.0\n")
    write_edits(path, {}, removals=["usage.show_cost"])
    text = path.read_text(encoding="utf-8")
    assert "show_cost" not in text
    assert "fx_rate" in text  # sibling key untouched


def test_write_edits_missing_removal_key_is_noop_not_error(write_config) -> None:
    path = write_config("usage:\n  fx_rate: 1.0\n")
    # Removing an absent key must not raise; the edit still applies.
    write_edits(path, {"usage.fx_rate": 3.0}, removals=["usage.show_cost"])
    assert load_config(path).usage.fx_rate == 3.0


# ----- apply_plan (reconcile -> writer wiring) ---------------------------------------


def test_apply_plan_rewrites_via_writer(write_config) -> None:
    path = write_config("usage:\n  show_cost: false\n")
    findings = scan_config_file(str(path))
    plan = build_plan(findings, _accept_all)
    apply_plan(str(path), plan)
    assert load_config(path).usage.mode == "off"
    assert "show_cost" not in path.read_text(encoding="utf-8")


# ----- CLI: clauster config reconcile -----------------------------------------------


def _cfg(write_config, extra: str):
    return str(write_config(extra))


def test_cli_reconcile_clean_config_returns_zero(write_config, capsys) -> None:
    path = _cfg(write_config, "claude:\n  launch_mode: pty\n")
    assert cli.main(["config", "reconcile", "-c", path, "--yes"]) == 0
    assert "no deprecated keys" in capsys.readouterr().err


def test_cli_reconcile_dry_run_writes_nothing(write_config, capsys) -> None:
    path = _cfg(write_config, "usage:\n  show_cost: false\n")
    before = open(path, encoding="utf-8").read()
    assert cli.main(["config", "reconcile", "-c", path, "--dry-run", "--yes"]) == 0
    assert open(path, encoding="utf-8").read() == before  # untouched
    assert "--dry-run" in capsys.readouterr().err


def test_cli_reconcile_yes_applies(write_config) -> None:
    path = _cfg(write_config, "claude:\n  resume_mode: pty\nusage:\n  show_cost: false\n")
    assert cli.main(["config", "reconcile", "-c", path, "--yes"]) == 0
    reloaded = load_config(path)
    assert reloaded.claude.launch_mode == "pty"
    assert reloaded.usage.mode == "off"
    text = open(path, encoding="utf-8").read()
    assert "resume_mode" not in text and "show_cost" not in text


def test_cli_reconcile_interactive_accept_and_skip(write_config, monkeypatch) -> None:
    path = _cfg(write_config, "claude:\n  resume_mode: pty\nusage:\n  show_cost: false\n")
    # Accept the first finding (empty line), skip the second ("n").
    monkeypatch.setattr("sys.stdin", io.StringIO("\nn\n"))
    assert cli.main(["config", "reconcile", "-c", path]) == 0
    text = open(path, encoding="utf-8").read()
    assert "resume_mode" not in text  # accepted -> removed
    assert "launch_mode: pty" in text
    assert "show_cost: false" in text  # skipped -> kept


def test_cli_reconcile_interactive_drop_only_proposal(write_config, monkeypatch, capsys) -> None:
    # show_cost: true is drop-only (no replacement value) — the interactive proposal
    # line reflects that, and accepting removes the key.
    path = _cfg(write_config, "usage:\n  show_cost: true\n")
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert cli.main(["config", "reconcile", "-c", path]) == 0
    assert "no replacement value needed" in capsys.readouterr().err
    assert "show_cost" not in open(path, encoding="utf-8").read()


def test_cli_reconcile_interactive_pick_value(write_config, monkeypatch) -> None:
    path = _cfg(write_config, "usage:\n  show_cost: false\n")
    monkeypatch.setattr("sys.stdin", io.StringIO("tokens\n"))
    assert cli.main(["config", "reconcile", "-c", path]) == 0
    assert load_config(path).usage.mode == "tokens"


def test_cli_reconcile_interactive_invalid_choice_skips(write_config, monkeypatch, capsys) -> None:
    path = _cfg(write_config, "usage:\n  show_cost: false\n")
    monkeypatch.setattr("sys.stdin", io.StringIO("bogus\n"))
    assert cli.main(["config", "reconcile", "-c", path]) == 0
    assert "show_cost: false" in open(path, encoding="utf-8").read()  # unchanged
    assert "not a valid choice" in capsys.readouterr().err


def test_cli_reconcile_eof_accepts_default(write_config, monkeypatch) -> None:
    path = _cfg(write_config, "usage:\n  show_cost: false\n")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # immediate EOF
    assert cli.main(["config", "reconcile", "-c", path]) == 0
    assert load_config(path).usage.mode == "off"


def test_cli_reconcile_all_skipped_changes_nothing(write_config, monkeypatch, capsys) -> None:
    path = _cfg(write_config, "usage:\n  show_cost: false\n")
    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))
    assert cli.main(["config", "reconcile", "-c", path]) == 0
    assert "nothing to change" in capsys.readouterr().err


def test_cli_config_without_subcommand_prints_help(capsys) -> None:
    assert cli.main(["config"]) == 2
    assert "reconcile" in capsys.readouterr().err


def test_cli_reconcile_missing_config_exits_2() -> None:
    with pytest.raises(SystemExit) as ei:
        cli.main(["config", "reconcile", "-c", "/no/such/clauster.yml"])
    assert ei.value.code == 2


def test_cli_reconcile_both_keys_present_removes_alias_keeps_replacement(write_config) -> None:
    path = _cfg(write_config, "claude:\n  launch_mode: standard\n  resume_mode: pty\n")
    assert cli.main(["config", "reconcile", "-c", path, "--yes"]) == 0
    text = open(path, encoding="utf-8").read()
    assert "resume_mode" not in text
    assert load_config(path).claude.launch_mode == "standard"  # existing value kept


def test_cli_reconcile_interactive_both_present_shows_kept_note(
    write_config, monkeypatch, capsys
) -> None:
    # The "already set — kept" proposal line is the interactive replacement_present branch.
    path = _cfg(write_config, "claude:\n  launch_mode: standard\n  resume_mode: pty\n")
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert cli.main(["config", "reconcile", "-c", path]) == 0
    assert "is already set — kept" in capsys.readouterr().err


def test_cli_reconcile_surfaces_rewrite_rejection(write_config, monkeypatch, capsys) -> None:
    # A failing rewrite must surface (fail closed), never be swallowed.
    path = _cfg(write_config, "usage:\n  show_cost: false\n")

    def boom(*_a, **_k):
        raise RuntimeError("validation blew up")

    monkeypatch.setattr("clauster.reconcile.apply_plan", boom)
    assert cli.main(["config", "reconcile", "-c", path, "--yes"]) == 1
    assert "rewrite rejected" in capsys.readouterr().err


def test_cli_reconcile_surfaces_oserror_on_rewrite(write_config, monkeypatch, capsys) -> None:
    path = _cfg(write_config, "usage:\n  show_cost: false\n")

    def disk_full(*_a, **_k):
        raise OSError("no space left on device")

    monkeypatch.setattr("clauster.reconcile.apply_plan", disk_full)
    assert cli.main(["config", "reconcile", "-c", path, "--yes"]) == 1
    assert "rewrite failed" in capsys.readouterr().err


def test_cli_reconcile_surfaces_read_error(write_config, monkeypatch, capsys) -> None:
    path = _cfg(write_config, "usage:\n  show_cost: false\n")

    def cant_read(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr("clauster.reconcile.scan_config_file", cant_read)
    assert cli.main(["config", "reconcile", "-c", path]) == 1
    assert "could not read config" in capsys.readouterr().err


def test_del_dotted_non_mapping_along_path_is_false(write_config) -> None:
    # A dotted path whose parent is a scalar (not a mapping) removes nothing.
    from clauster.config_writer import _del_dotted

    doc = {"claude": "scalar-not-a-map"}
    assert _del_dotted(doc, "claude.launch_mode") is False
    assert doc == {"claude": "scalar-not-a-map"}
