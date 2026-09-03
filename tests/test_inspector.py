from __future__ import annotations

import json
from pathlib import Path

from clauster import inspector
from clauster.models import Attribution


def _agent(pid, cwd, sid, kind="interactive", started=1716998400000, state=None):
    item = {
        "pid": pid,
        "cwd": cwd,
        "kind": kind,
        "startedAt": started,
        "sessionId": sid,
    }
    if state is not None:
        item["state"] = state
    return item


def test_parse_agents_json_empty():
    assert inspector.parse_agents_json("") == []
    assert inspector.parse_agents_json("[]") == []


def test_parse_agents_json_skips_malformed():
    payload = json.dumps([_agent(1, "/a", "uuid-1"), {"pid": 2}])  # second lacks fields
    sessions = inspector.parse_agents_json(payload)
    assert len(sessions) == 1
    assert sessions[0].pid == 1
    assert sessions[0].local_uuid == "uuid-1"


def test_parse_agents_json_tolerates_unexpected_shapes():
    # Valid JSON but an unexpected shape must return [] rather than crashing: a
    # top-level scalar/None/bool would raise AttributeError on ``.get``, and a dict
    # whose agents/sessions value isn't a list would raise TypeError on iteration.
    # (fail-closed liveness holds: malformed JSON still raises at json.loads.)
    assert inspector.parse_agents_json("5") == []
    assert inspector.parse_agents_json('"a string"') == []
    assert inspector.parse_agents_json("true") == []
    assert inspector.parse_agents_json("null") == []
    # dict with a non-list agents/sessions value (CodeRabbit catch)
    assert inspector.parse_agents_json('{"agents": null}') == []
    assert inspector.parse_agents_json('{"agents": 5}') == []
    assert inspector.parse_agents_json('{"sessions": "nope"}') == []
    assert inspector.parse_agents_json("{}") == []  # neither key present


def test_parse_agents_json_deep_nesting_converts_to_jsondecode():
    # Deeply-nested JSON overflows CPython's recursive scanner and raises RecursionError on
    # every supported interpreter; parse_agents_json converts that to JSONDecodeError so
    # callers that already handle the strict-parse failure (e.g. the runner cross-check)
    # degrade uniformly rather than on a stray RecursionError. This exercises the conversion
    # branch; the monkeypatch test below pins it deterministically.
    try:
        inspector.parse_agents_json("[" * 100_000)
    except json.JSONDecodeError:
        pass
    else:
        raise AssertionError("expected JSONDecodeError on deeply-nested JSON")


def test_parse_agents_json_recursion_error_converts_to_jsondecode(monkeypatch):
    # Pins the RecursionError -> JSONDecodeError conversion directly with a shallow input,
    # independent of the scanner's actual overflow depth: the arm converts RecursionError to
    # JSONDecodeError so callers see one strict-parse failure. Mirrors the config_write
    # guard for the same handler shape (PR 1408).
    def _overflow(_text):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(inspector.json, "loads", _overflow)
    try:
        inspector.parse_agents_json("[[1]]")
    except json.JSONDecodeError as exc:
        assert "recursion" in str(exc).lower()
    else:
        raise AssertionError("expected JSONDecodeError from the RecursionError handler")


def test_reconcile_attributes_by_resolved_cwd(tmp_path: Path):
    proj = tmp_path / "alpha"
    proj.mkdir()
    sessions = inspector.parse_agents_json(
        json.dumps([_agent(10, str(proj), "u-tracked"), _agent(11, "/somewhere/else", "u-ext")])
    )
    result = inspector.reconcile(sessions, {proj: ["alpha"]})
    by_uuid = {s.local_uuid: s for s in result}
    assert by_uuid["u-tracked"].attribution is Attribution.TRACKED
    assert by_uuid["u-tracked"].parent_instance == "alpha"
    assert by_uuid["u-ext"].attribution is Attribution.EXTERNAL


def test_reconcile_normalizes_trailing_slash(tmp_path: Path):
    proj = tmp_path / "beta"
    proj.mkdir()
    sessions = inspector.parse_agents_json(json.dumps([_agent(12, str(proj) + "/", "u")]))
    result = inspector.reconcile(sessions, {proj: ["beta"]})
    assert result[0].attribution is Attribution.TRACKED


def test_reconcile_worktree_session_attributed_by_containment(tmp_path: Path):
    # `claude remote-control --spawn worktree` runs each session in a per-session
    # worktree under `<root>/.claude/worktrees/<id>`, which never exactly matches the
    # project-root key. It must still attribute to the bridge by containment, or the
    # dashboard shows no live-session count for a worktree bridge (the #570 feature).
    proj = tmp_path / "alpha"
    wt = proj / ".claude" / "worktrees" / "bridge-cse_x"
    wt.mkdir(parents=True)
    sessions = inspector.parse_agents_json(json.dumps([_agent(10, str(wt), "u-wt")]))
    result = inspector.reconcile(sessions, {proj: ["alpha"]}, worktree_roots={proj: ["alpha"]})
    assert result[0].attribution is Attribution.TRACKED
    assert result[0].parent_instance == "alpha"


def test_reconcile_worktree_containment_is_opt_in(tmp_path: Path):
    # Containment is per worktree-spawn bridge only. A session in a subdir of a
    # NON-worktree project (absent from worktree_roots) stays EXTERNAL — a stray
    # `claude` in a project subdir must not be claimed as the bridge's session.
    proj = tmp_path / "alpha"
    sub = proj / "src"
    sub.mkdir(parents=True)
    sessions = inspector.parse_agents_json(json.dumps([_agent(10, str(sub), "u-sub")]))
    result = inspector.reconcile(sessions, {proj: ["alpha"]})  # no worktree_roots
    assert result[0].attribution is Attribution.EXTERNAL
    assert result[0].parent_instance is None


def test_reconcile_worktree_containment_scoped_to_worktrees_dir(tmp_path: Path):
    # Even for a worktree-spawn bridge, containment is the `.claude/worktrees` subtree
    # ONLY — a stray interactive `claude` in another subdir of the project (here
    # `<root>/src`) is not where `--spawn worktree` puts sessions and must stay
    # EXTERNAL, so it doesn't inflate the bridge's live-session count.
    proj = tmp_path / "alpha"
    sub = proj / "src"
    sub.mkdir(parents=True)
    sessions = inspector.parse_agents_json(json.dumps([_agent(10, str(sub), "u-stray")]))
    result = inspector.reconcile(sessions, {proj: ["alpha"]}, worktree_roots={proj: ["alpha"]})
    assert result[0].attribution is Attribution.EXTERNAL
    assert result[0].parent_instance is None


def test_reconcile_worktree_nested_root_most_specific_wins(tmp_path: Path):
    # Nested worktree projects: a session under the inner root attributes to the
    # inner bridge, not the ancestor project that also contains it.
    outer = tmp_path / "outer"
    inner = outer / "inner"
    wt = inner / ".claude" / "worktrees" / "bridge-y"
    wt.mkdir(parents=True)
    sessions = inspector.parse_agents_json(json.dumps([_agent(10, str(wt), "u-nested")]))
    result = inspector.reconcile(
        sessions,
        {outer: ["outer"], inner: ["inner"]},
        worktree_roots={outer: ["outer"], inner: ["inner"]},
    )
    assert result[0].attribution is Attribution.TRACKED
    assert result[0].parent_instance == "inner"


def test_reconcile_worktree_containment_respects_kind_gate(tmp_path: Path):
    # The kind gate still applies under containment: a `claude --bg` session inside a
    # worktree-spawn project's tree is NOT the bridge's session.
    proj = tmp_path / "alpha"
    wt = proj / ".claude" / "worktrees" / "bridge-z"
    wt.mkdir(parents=True)
    sessions = inspector.parse_agents_json(
        json.dumps([_agent(10, str(wt), "u-bg", kind="background", state="working")])
    )
    result = inspector.reconcile(sessions, {proj: ["alpha"]}, worktree_roots={proj: ["alpha"]})
    assert result[0].attribution is Attribution.UNTRACKED


def test_reconcile_hosted_at_worktree_root_stays_hosted(tmp_path: Path):
    # A pre-CT-1 hosted session (no agent pid) sits at the project ROOT, not in
    # `.claude/worktrees`, so worktree containment must not claim it — it still
    # resolves HOSTED via the cwd fallback even when a worktree root is supplied.
    proj = tmp_path / "alpha"
    proj.mkdir()
    sessions = inspector.parse_agents_json(json.dumps([_agent(10, str(proj), "u-hosted")]))
    result = inspector.reconcile(
        sessions, {}, hosted_cwds={proj: "host-9"}, worktree_roots={proj: ["alpha"]}
    )
    assert result[0].attribution is Attribution.HOSTED
    assert result[0].parent_instance == "host-9"


def test_parse_agents_json_drops_terminal_states():
    # Agent view (2.1.139+) can list finished sessions; done/failed/stopped are
    # not working sessions and must not count as live anywhere.
    payload = json.dumps(
        [
            _agent(1, "/a", "u-working", state="working"),
            _agent(2, "/a", "u-blocked", state="blocked"),
            _agent(3, "/a", "u-legacy"),  # pre-agent-view item: no state field
            _agent(4, "/a", "u-done", state="done"),
            _agent(5, "/a", "u-failed", state="failed"),
            _agent(6, "/a", "u-stopped", state="stopped"),
        ]
    )
    sessions = inspector.parse_agents_json(payload)
    assert [s.local_uuid for s in sessions] == ["u-working", "u-blocked", "u-legacy"]
    assert sessions[0].state == "working"
    assert sessions[2].state == ""


def test_reconcile_background_kind_never_tracked(tmp_path: Path):
    # A `claude --bg` session in a managed project's cwd is NOT the bridge's
    # session — attributing it TRACKED would be a false liveness signal.
    proj = tmp_path / "alpha"
    proj.mkdir()
    sessions = inspector.parse_agents_json(
        json.dumps([_agent(10, str(proj), "u-bg", kind="background", state="working")])
    )
    result = inspector.reconcile(sessions, {proj: ["alpha"]})
    assert result[0].attribution is Attribution.UNTRACKED
    assert result[0].parent_instance is None


def test_reconcile_background_kind_never_external():
    # EXTERNAL would phantom-delete a stopped managed record and surface
    # "external session active" for what is not a bridge.
    sessions = inspector.parse_agents_json(
        json.dumps([_agent(11, "/somewhere/else", "u-bg", kind="background")])
    )
    result = inspector.reconcile(sessions, {})
    assert result[0].attribution is Attribution.UNTRACKED


def test_reconcile_unknown_kind_stays_untracked(tmp_path: Path):
    # Allowlist, not blocklist: a future kind doesn't join either (fail-closed).
    proj = tmp_path / "alpha"
    proj.mkdir()
    sessions = inspector.parse_agents_json(
        json.dumps([_agent(12, str(proj), "u-new", kind="subagent")])
    )
    result = inspector.reconcile(sessions, {proj: ["alpha"]})
    assert result[0].attribution is Attribution.UNTRACKED


def test_reconcile_missing_kind_still_joins(tmp_path: Path):
    # Pre-agent-view CLI compat: an item without `kind` still attributes by cwd.
    proj = tmp_path / "alpha"
    proj.mkdir()
    item = _agent(13, str(proj), "u-old")
    del item["kind"]
    result = inspector.reconcile(
        inspector.parse_agents_json(json.dumps([item])), {proj: ["alpha"]}
    )
    assert result[0].attribution is Attribution.TRACKED


def test_reconcile_hosted_by_pid(tmp_path: Path):
    # A Clauster hosted session is identified by its CT-1 agent pid → HOSTED, not
    # EXTERNAL (#592). Its workspace has no managed bridge, so cwd alone wouldn't help.
    proj = tmp_path / "ws"
    sessions = inspector.parse_agents_json(json.dumps([_agent(46620, str(proj), "u-hosted")]))
    result = inspector.reconcile(sessions, {}, hosted_pids={46620: "host-1"})
    assert result[0].attribution is Attribution.HOSTED
    assert result[0].parent_instance == "host-1"


def test_reconcile_hosted_pid_match_overrides_kind(tmp_path: Path):
    # pid is authoritative identity: a hosted session is claimed before the kind gate,
    # so even a non-bridge kind reads as HOSTED rather than slipping to UNTRACKED.
    sessions = inspector.parse_agents_json(
        json.dumps([_agent(7, "/anywhere", "u-h", kind="background", state="working")])
    )
    result = inspector.reconcile(sessions, {}, hosted_pids={7: "host-2"})
    assert result[0].attribution is Attribution.HOSTED
    assert result[0].parent_instance == "host-2"


def test_reconcile_hosted_by_cwd_fallback(tmp_path: Path):
    # Pre-CT-1 daemon: no agent pid to match, so a hosted session attributes by its
    # workspace cwd instead of falling through to EXTERNAL.
    proj = tmp_path / "ws"
    proj.mkdir()
    sessions = inspector.parse_agents_json(json.dumps([_agent(50, str(proj), "u-h")]))
    result = inspector.reconcile(sessions, {}, hosted_cwds={proj: "host-3"})
    assert result[0].attribution is Attribution.HOSTED
    assert result[0].parent_instance == "host-3"


def test_reconcile_managed_bridge_wins_over_hosted_cwd(tmp_path: Path):
    # A real bridge at a cwd that also appears in hosted_cwds stays TRACKED — the
    # managed-bridge join is checked before the hosted cwd fallback.
    proj = tmp_path / "ws"
    proj.mkdir()
    sessions = inspector.parse_agents_json(json.dumps([_agent(60, str(proj), "u-b")]))
    result = inspector.reconcile(sessions, {proj: ["bridge-1"]}, hosted_cwds={proj: "host-4"})
    assert result[0].attribution is Attribution.TRACKED
    assert result[0].parent_instance == "bridge-1"


def test_reconcile_hosted_args_default_to_external(tmp_path: Path):
    # Without hosted info, a session at an unmanaged cwd is still EXTERNAL (the new
    # parameters are optional and don't change pre-existing attribution).
    sessions = inspector.parse_agents_json(json.dumps([_agent(70, "/elsewhere", "u-e")]))
    result = inspector.reconcile(sessions, {})
    assert result[0].attribution is Attribution.EXTERNAL


def test_reconcile_external_session_at_bridge_cwd_stays_external(tmp_path: Path):
    # #820: an external SSH/terminal `claude` run by hand IN a managed bridge's dir
    # shares the bridge's cwd. With the ownership gate, its unowned worker pid keeps it
    # EXTERNAL instead of being folded into the bridge's tracked sessions — even though
    # a genuine bridge child at the same cwd (owned pid) is TRACKED.
    proj = tmp_path / "alpha"
    proj.mkdir()
    sessions = inspector.parse_agents_json(
        json.dumps([_agent(100, str(proj), "u-owned"), _agent(200, str(proj), "u-ext")])
    )
    result = inspector.reconcile(
        sessions, {proj: ["alpha"]}, owned_pids_by_instance={"alpha": {100}}
    )
    by_uuid = {s.local_uuid: s for s in result}
    assert by_uuid["u-owned"].attribution is Attribution.TRACKED
    assert by_uuid["u-owned"].parent_instance == "alpha"
    assert by_uuid["u-ext"].attribution is Attribution.EXTERNAL
    assert by_uuid["u-ext"].parent_instance is None


def test_reconcile_ownership_gate_none_is_cwd_only(tmp_path: Path):
    # owned_pids=None disables the gate (legacy/back-compat) — a session at a managed
    # cwd is TRACKED on cwd alone, exactly as before the gate existed.
    proj = tmp_path / "alpha"
    proj.mkdir()
    sessions = inspector.parse_agents_json(json.dumps([_agent(300, str(proj), "u-x")]))
    result = inspector.reconcile(sessions, {proj: ["alpha"]})  # no owned_pids_by_instance
    assert result[0].attribution is Attribution.TRACKED


def test_reconcile_ownership_gate_absent_instance_is_cwd_only(tmp_path: Path):
    # An instance ABSENT from owned_pids_by_instance is not gated — cwd-only. This is the
    # STARTING-bridge window (#713): the bridge's pid isn't known yet, so its
    # auto-created initial session must still attribute rather than flicker EXTERNAL.
    proj = tmp_path / "alpha"
    proj.mkdir()
    sessions = inspector.parse_agents_json(json.dumps([_agent(350, str(proj), "u-s")]))
    # gate enabled (non-None), but keyed for a DIFFERENT instance → alpha is ungated.
    result = inspector.reconcile(
        sessions, {proj: ["alpha"]}, owned_pids_by_instance={"other": {1}}
    )
    assert result[0].attribution is Attribution.TRACKED


def test_reconcile_ownership_gate_empty_set_marks_external(tmp_path: Path):
    # An instance present with an EMPTY owned set (bridge pid known, but it owns none of
    # the observed pids) actively marks a session at that cwd EXTERNAL — distinct from an
    # absent instance (cwd-only). Guards against collapsing "known-but-owns-nothing" into
    # "unknown → attribute anyway."
    proj = tmp_path / "alpha"
    proj.mkdir()
    sessions = inspector.parse_agents_json(json.dumps([_agent(400, str(proj), "u-y")]))
    result = inspector.reconcile(
        sessions, {proj: ["alpha"]}, owned_pids_by_instance={"alpha": set()}
    )
    assert result[0].attribution is Attribution.EXTERNAL


def test_reconcile_worktree_join_ungated_when_only_one_bridge_could_own_it(tmp_path: Path):
    # The worktree join is gated ONLY when there is something to disambiguate. With one
    # worktree bridge at this root, containment already names the owner, so an unowned pid
    # still attributes. That is deliberate coverage insurance: where the process tree can't
    # be walked (AccessDenied under hidepid / a hardened container) `owned_pids` returns
    # just the bridge pid, and gating here would flip every worktree session to EXTERNAL —
    # where they'd show up NOWHERE, since the external grouping joins on the exact project
    # path and a worktree cwd never matches it. That would undo #1076.
    proj = tmp_path / "alpha"
    wt = proj / ".claude" / "worktrees" / "bridge-cse_x"
    wt.mkdir(parents=True)
    sessions = inspector.parse_agents_json(json.dumps([_agent(500, str(wt), "u-wt")]))
    result = inspector.reconcile(
        sessions,
        {proj: ["alpha"]},
        worktree_roots={proj: ["alpha"]},
        owned_pids_by_instance={"alpha": set()},
    )
    assert result[0].attribution is Attribution.TRACKED
    assert result[0].parent_instance == "alpha"


def test_reconcile_worktree_join_gated_once_several_bridges_share_the_subtree(tmp_path: Path):
    # With N worktree bridges under one root, containment says only "some bridge here" —
    # ownership is the only thing that can name which, so it becomes load-bearing (#1020
    # A3). Two RUNNING bridges that both know their pids and neither owns this one must
    # not have it handed to whichever was registered first.
    proj = tmp_path / "alpha"
    wt = proj / ".claude" / "worktrees" / "bridge-cse_x"
    wt.mkdir(parents=True)
    sessions = inspector.parse_agents_json(json.dumps([_agent(500, str(wt), "u-wt")]))
    result = inspector.reconcile(
        sessions,
        {proj: ["pty-1", "pty-2"]},
        worktree_roots={proj: ["pty-1", "pty-2"]},
        owned_pids_by_instance={"pty-1": {1}, "pty-2": {2}},
    )
    assert result[0].attribution is Attribution.EXTERNAL


def test_reconcile_worktree_starting_bridge_claims_beside_a_keyed_sibling(tmp_path: Path):
    # A worktree root with a RUNNING bridge and a still-pid-less STARTING one. The
    # STARTING bridge's own first worker is owned by nobody yet, and on this arm refusing
    # to attribute is NOT the safe answer: the external grouping joins on the exact
    # project path, which a worktree cwd never matches, so EXTERNAL here renders the
    # session NOWHERE — not under a bridge, not in the external list. The pid-less
    # candidate (the bridge that just spawned) therefore claims it.
    #
    # Contrast `test_reconcile_starting_bridge_cannot_absorb_an_external_when_a_sibling_
    # is_gated`: at an exact cwd the same shape stays EXTERNAL, because there EXTERNAL is
    # visible and #820's hand-run `claude` lives at exactly that path.
    proj = tmp_path / "alpha"
    wt = proj / ".claude" / "worktrees" / "bridge-new"
    wt.mkdir(parents=True)
    sessions = inspector.parse_agents_json(json.dumps([_agent(777, str(wt), "u-fresh")]))
    result = inspector.reconcile(
        sessions,
        {proj: ["running-1", "starting-2"]},
        worktree_roots={proj: ["running-1", "starting-2"]},
        owned_pids_by_instance={"running-1": {200}},  # starting-2 has no pid yet
    )
    assert result[0].attribution is Attribution.TRACKED
    assert result[0].parent_instance == "starting-2"


def test_reconcile_starting_bridge_cannot_absorb_an_external_when_a_sibling_is_gated(
    tmp_path: Path,
):
    # #820 vs #713, the mixed case. A RUNNING bridge (pid known) shares a cwd with a
    # STARTING one that has no pid yet. An operator's hand-run `claude` is owned by
    # neither — and must NOT be handed to the STARTING bridge just because that bridge
    # can't prove anything yet, which would hide a genuinely external session from the
    # external list and the Adopt affordance.
    #
    # The gate is decided for the CWD, not per candidate: any candidate here with a known
    # pid set means this location can prove ownership. That is what the pre-#1020 code did
    # by unioning co-located roots per cwd, so this preserves its strictness.
    proj = tmp_path / "alpha"
    proj.mkdir()
    sessions = inspector.parse_agents_json(json.dumps([_agent(999, str(proj), "u-handrun")]))
    result = inspector.reconcile(
        sessions,
        {proj: ["running-1", "starting-2"]},
        owned_pids_by_instance={"running-1": {200}},  # starting-2 has no pid yet
    )
    assert result[0].attribution is Attribution.EXTERNAL
    assert result[0].parent_instance is None


def test_reconcile_starting_bridge_still_claims_its_session_when_nothing_is_gated(
    tmp_path: Path,
):
    # The other half of the same rule — #713 must survive it. When NO candidate at this
    # cwd can prove ownership yet (every bridge here is still starting, pre-sidecar), the
    # startup window applies and the auto-created session attributes rather than
    # flickering EXTERNAL.
    proj = tmp_path / "alpha"
    proj.mkdir()
    sessions = inspector.parse_agents_json(json.dumps([_agent(500, str(proj), "u-initial")]))
    result = inspector.reconcile(
        sessions,
        {proj: ["starting-1"]},
        owned_pids_by_instance={"unrelated": {1}},  # keyed, but not for this cwd
    )
    assert result[0].attribution is Attribution.TRACKED
    assert result[0].parent_instance == "starting-1"


def test_reconcile_pidless_colocated_row_cannot_absorb_an_external(tmp_path: Path):
    # #820 regression guard for the per-instance rewrite. `_select_owner` falls back to a
    # candidate that is ABSENT from the ownership map (the #713 startup window). A row with
    # no pid that isn't starting — a crashed/ERROR spawn, or a `_stopped_from_persisted`
    # phantom — is exactly such a candidate, and would otherwise become a permanent sink
    # that re-labels an operator's hand-run `claude` as a dead bridge's session. The runner
    # keeps those rows out of the candidate lists (`_can_own_sessions`); this pins the
    # consequence at the reconcile layer so the two can't drift apart silently.
    proj = tmp_path / "alpha"
    proj.mkdir()
    sessions = inspector.parse_agents_json(json.dumps([_agent(999, str(proj), "u-handrun")]))
    result = inspector.reconcile(
        sessions,
        {proj: ["pty-live"]},  # the phantom is NOT a candidate
        owned_pids_by_instance={"pty-live": {200}},
    )
    assert result[0].attribution is Attribution.EXTERNAL
    assert result[0].parent_instance is None


def test_reconcile_separates_colocated_bridges_by_ownership(tmp_path: Path):
    # The #1020 A3 regression. A project runs a standard bridge plus two interactive
    # (worktree) bridges. Every session must land under the bridge that actually owns
    # it — before this, all three shared one project-keyed bucket, so the standard
    # bridge's row listed the independent interactive sessions as if it owned them.
    proj = tmp_path / "alpha"
    wt1 = proj / ".claude" / "worktrees" / "bridge-one"
    wt2 = proj / ".claude" / "worktrees" / "bridge-two"
    wt1.mkdir(parents=True)
    wt2.mkdir(parents=True)
    sessions = inspector.parse_agents_json(
        json.dumps(
            [
                _agent(10, str(proj), "u-standard"),
                _agent(20, str(wt1), "u-pty-one"),
                _agent(30, str(wt2), "u-pty-two"),
            ]
        )
    )
    result = inspector.reconcile(
        sessions,
        {proj: ["std-1", "pty-1", "pty-2"]},
        worktree_roots={proj: ["pty-1", "pty-2"]},
        owned_pids_by_instance={"std-1": {10}, "pty-1": {20}, "pty-2": {30}},
    )
    by_uuid = {s.local_uuid: s for s in result}
    assert by_uuid["u-standard"].parent_instance == "std-1"
    assert by_uuid["u-pty-one"].parent_instance == "pty-1"
    assert by_uuid["u-pty-two"].parent_instance == "pty-2"
    assert all(s.attribution is Attribution.TRACKED for s in result)
