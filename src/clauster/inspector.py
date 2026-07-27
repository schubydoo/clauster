"""`claude agents --json` cross-check (spec §7, observability source #2).

Secondary, ~5-min cadence. The JSON is a flat list of working sessions with no
bridge/env grouping (Capture B), so attribution joins on ``cwd`` — the only link
back to a managed bridge: an exact match to a bridge's cwd, or (for worktree-spawn
bridges, whose sessions run in per-session worktrees under the project) containment
in the ``.claude/worktrees`` subtree. ``sessionId`` here is the local RFC-4122 UUID,
never the API ULID.

Agent view (Claude Code 2.1.139+) lists `claude --bg` background sessions in the
same output, tagged ``kind: "background"`` and carrying a lifecycle ``state``.
The cwd join is therefore gated on ``kind`` (a background session in a managed
project's dir is not the bridge's session) and terminal-state entries are
dropped at parse (a finished session is not a working session).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import procutil
from .claude_cli import resolve_binary
from .models import Attribution, WorkingSession
from .pointers import WORKTREE_SUBDIR

# Agent-view lifecycle states that mean the session is over — not live anywhere,
# neither for bridge attribution nor for the ghost-reaper's keep set.
_TERMINAL_STATES = frozenset({"done", "failed", "stopped"})
# Kinds eligible for the cwd→bridge join. Bridge child sessions are observed
# "interactive"; "" tolerates a pre-agent-view CLI that omits the field. Anything
# else ("background", future kinds) is allowlisted out — fail-closed attribution.
_BRIDGE_KINDS = frozenset({"", "interactive"})
# A worktree bridge's sessions live in this subtree, never at the root, so containment
# attribution matches HERE specifically rather than the whole project tree — a stray
# interactive `claude` run by hand elsewhere under the project must not be claimed as the
# bridge's session. Shared with `usage` (see the definition) so the two can't disagree.
_WORKTREE_SUBDIR = WORKTREE_SUBDIR


def list_working_sessions(binary: str, *, timeout: float = 10.0) -> list[WorkingSession]:
    """Invoke ``claude agents --json`` and parse the working-session list.

    Blocking — callers run it via ``asyncio.to_thread``.

    **Never ``--all``.** That flag also returns *completed* background sessions, and since
    #1116 this list feeds the phantom-prune, which DELETES a resumable card when a session
    looks like a live unmanaged bridge. A finished session is not evidence of a live one.
    :func:`parse_agents_json` drops terminal states as a second guard, but that guard is a
    no-op against a pre-2.1.139 CLI (no ``state`` field, so it defaults to ``""``) — on
    those, omitting ``--all`` is the only thing keeping completed sessions out.
    """
    resolved = resolve_binary(binary)
    proc = subprocess.run(
        [resolved, "agents", "--json"],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=procutil.child_env(),
        check=True,
    )
    return parse_agents_json(proc.stdout)


def parse_agents_json(stdout: str) -> list[WorkingSession]:
    """Parse the JSON array; tolerate empty output / unexpected shape, skip malformed items."""
    text = stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except RecursionError as exc:
        # Deeply-nested JSON overflows CPython's recursive scanner before json can
        # raise JSONDecodeError; surface it as the same strict "unparseable" failure
        # so callers already handling JSONDecodeError (e.g. the runner's best-effort
        # cross-check, runner.py) degrade uniformly instead of a stray RecursionError.
        raise json.JSONDecodeError("Exceeded maximum recursion depth", text, 0) from exc
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("agents", data.get("sessions"))
    else:
        items = None
    if not isinstance(items, list):
        # Valid JSON but an unexpected shape — a top-level scalar/None, or a dict
        # whose agents/sessions value isn't a list ({"agents": null}, {"agents": 5}).
        # Return empty rather than raising AttributeError on ``.get`` or TypeError on
        # iteration. (A genuinely malformed payload still fails at ``json.loads`` above,
        # which stays strict by design — fail-closed liveness.)
        return []
    sessions: list[WorkingSession] = []
    for item in items:
        try:
            session = WorkingSession.from_agents_json(item)
        except (KeyError, TypeError, ValueError):
            continue
        if session.state in _TERMINAL_STATES:
            continue
        sessions.append(session)
    return sessions


def _select_owner(
    candidates: list[str],
    pid: int,
    owned_by_instance: dict[str, set[int]] | None,
    *,
    unproven_may_claim: bool = False,
) -> str | None:
    """Pick which of several co-located bridges owns ``pid``, or ``None`` (#820, #1020).

    Several bridges can share one cwd — a standard bridge plus N interactive (pty)
    bridges at the same project root — so a cwd identifies a *set* of candidate
    instances, never one. Process ownership is what separates them: each candidate's
    own live pid(s) plus their descendants.

    ``None`` for ``owned_by_instance`` disables the gate (legacy callers) and the first
    candidate wins. Otherwise a candidate that provably owns the pid wins.

    Failing that, the gate is decided **for the cwd as a whole, not per candidate**: if
    ANY candidate here has a known pid set, this location can prove ownership, so a pid
    none of them owns is not ours and matches nothing — an external ``claude`` run by
    hand in a managed bridge's directory stays EXTERNAL (#820). Only when NO candidate
    can prove anything yet (every bridge here is still starting, pre-sidecar) does the
    #713 window apply and a pid-less candidate claim the session, so a just-spawned
    bridge's auto-created session doesn't flicker EXTERNAL.

    Deciding this per candidate instead would reopen #820 (the pre-#1020 code gated per
    cwd, unioning every co-located bridge's roots): a RUNNING bridge sharing a cwd with a
    pid-less STARTING one would leave the STARTING bridge as an ungated candidate that
    absorbs any unowned pid — hiding a genuinely external session from the external list
    and the Adopt affordance. Per-cwd gating with per-instance attribution keeps #820's
    strictness and still names the right owner.

    ``unproven_may_claim`` relaxes exactly that last step, and ONLY the worktree caller
    passes it. The two arms differ in what "no match" costs. At an exact cwd, EXTERNAL is
    a *visible* answer — the session shows up in the external-session list with its Adopt
    affordance — so refusing to guess is genuinely conservative, and #820's hand-run
    ``claude`` lives at exactly that path. Inside ``.claude/worktrees/…`` it is not:
    the external grouping joins on the exact project path, which a worktree cwd never
    matches, so EXTERNAL there means the session renders NOWHERE. Fail-closed stops being
    conservative and becomes a disappearing act.

    With the flag, a candidate that cannot yet prove anything may claim the session even
    beside a keyed sibling. That is well-motivated rather than a coin flip: the pid-less
    candidate is a bridge that *just* spawned, which is precisely who a brand-new worker
    in this subtree is likely to belong to. It does not weaken #820 — that subtree is
    created and owned by the bridge, not somewhere an operator runs ``claude`` by hand,
    which is why the pre-#1020 code left the worktree arm ungated altogether. A candidate
    that IS keyed and simply doesn't own the pid still never claims it, so two running
    worktree bridges can't absorb each other's sessions (the #1020 A3 lie in miniature).
    """
    if not candidates:
        return None
    if owned_by_instance is None:
        return candidates[0]
    for inst_id in candidates:
        owned = owned_by_instance.get(inst_id)
        if owned is not None and pid in owned:
            return inst_id
    unproven = [inst_id for inst_id in candidates if inst_id not in owned_by_instance]
    if not unproven:
        # Every candidate here can prove ownership and none owns this pid: it isn't ours.
        return None
    if unproven_may_claim or len(unproven) == len(candidates):
        # #713. Registration order, so a tie between two still-starting bridges is stable.
        return unproven[0]
    return None


def reconcile(
    sessions: list[WorkingSession],
    managed_cwds: dict[Path, list[str]],
    hosted_pids: dict[int, str] | None = None,
    hosted_cwds: dict[Path, str] | None = None,
    worktree_roots: dict[Path, list[str]] | None = None,
    owned_pids_by_instance: dict[str, set[int]] | None = None,
) -> list[WorkingSession]:
    """Attribute each working session to the managed bridge INSTANCE that owns it.

    ``managed_cwds`` maps a resolved project path → the instance ids of the bridges
    running there. Sessions whose cwd matches become TRACKED (and carry
    ``parent_instance``); the rest are EXTERNAL (a bridge/session Clauster doesn't
    manage). Non-bridge kinds never join: a `claude --bg` session sharing a managed cwd
    must not read as the bridge's session (TRACKED = false liveness) nor as an unmanaged
    bridge (EXTERNAL phantom-deletes a stopped record) — it stays UNTRACKED.

    The value is a LIST, and ``parent_instance`` is an ``instance_id``, because a project
    may run several bridges at once (#778). Keying either by project would fold every
    bridge on a project into one bucket, so the dashboard's standard-bridge row would
    list the independent interactive sessions as if it owned them (#1020 symptom A3).

    ``owned_pids_by_instance`` maps an instance id → the pids that bridge owns — its own
    live process pid(s) plus their descendants — and is both the #820 ownership gate and
    the tie-breaker between co-located bridges; see :func:`_select_owner`. (A managed
    session's ``agents --json`` pid is a bridge child, or for an in-process flag-form pty
    the bridge pid itself; the runner unions both.) cwd alone over-matches: an external
    SSH/terminal ``claude`` run by hand *in a managed bridge's directory* shares that cwd
    and would be folded into the bridge's tracked sessions, hiding its EXTERNAL status.
    ``None`` disables the gate entirely (legacy); the runner always passes the map, keyed
    only for bridges with a resolvable pid, so production is gated where it can be.

    ``worktree_roots`` maps a *worktree-spawn* bridge's resolved project root → the
    instance ids of the worktree bridges there. ``claude remote-control --spawn worktree``
    runs each session in a per-session git worktree under ``<root>/.claude/worktrees/…``,
    so the session cwd never exactly matches the project-root key in ``managed_cwds`` and
    would wrongly read as EXTERNAL — hiding every session under a worktree bridge from the
    dashboard. Such a session is attributed by *containment* in that ``.claude/worktrees``
    subtree (not the whole project, so a stray ``claude`` run by hand elsewhere under the
    project still reads EXTERNAL), most-specific root first so a nested project's bridge
    wins, and then narrowed to the owning instance by the same pid gate. Only
    worktree-spawn bridges opt in; same-dir/session bridges keep the exact-cwd join (their
    sessions share the bridge cwd).

    Clauster's own hosted (claustrum) sessions are spawned by it but run no bridge
    process, so they would otherwise fall through to EXTERNAL/unmanaged (#592).
    ``hosted_pids`` (agent pid → hosted id) and ``hosted_cwds`` (resolved cwd →
    hosted id) reclassify them as HOSTED: pid is the authoritative identity (a
    claustrum CT-1 ``agent_pid``) and is checked before the kind gate so a hosted
    session reads as HOSTED under any kind; cwd is the pre-CT-1 fallback (no pid to
    match) and joins only on a bridge kind, after the managed-bridge join so a real
    bridge at a shared cwd still wins.
    """
    resolved = {p.resolve(): list(inst_ids) for p, inst_ids in managed_cwds.items()}
    # `is None` (not `or {}`): the contract is about an omitted arg, not an empty one,
    # and a fresh local avoids rebinding the parameter.
    hosted_by_pid = hosted_pids if hosted_pids is not None else {}
    hosted_by_cwd = {
        p.resolve(): hid for p, hid in (hosted_cwds if hosted_cwds is not None else {}).items()
    }
    # Match the `.claude/worktrees` subtree of each worktree-spawn root, most-specific
    # (deepest) first: a session under a nested worktree project must attribute to the
    # inner bridge, not an ancestor project that contains it.
    worktree_dirs = sorted(
        (
            ((p / _WORKTREE_SUBDIR).resolve(), list(inst_ids))
            for p, inst_ids in (worktree_roots if worktree_roots is not None else {}).items()
        ),
        key=lambda kv: len(kv[0].parts),
        reverse=True,
    )
    for s in sessions:
        hosted_id = hosted_by_pid.get(s.pid)
        if hosted_id is not None:
            s.parent_instance = hosted_id
            s.attribution = Attribution.HOSTED
            continue
        if s.kind not in _BRIDGE_KINDS:
            s.attribution = Attribution.UNTRACKED
            continue
        cwd = s.cwd.resolve()
        # Exact-cwd join, narrowed to the ONE co-located bridge that owns this worker.
        # An unowned pid matches nothing here and falls through to EXTERNAL (#820).
        inst_id = _select_owner(resolved.get(cwd, []), s.pid, owned_pids_by_instance)
        if inst_id is not None:
            s.parent_instance = inst_id
            s.attribution = Attribution.TRACKED
            continue
        # Worktree containment, narrowed the same way: N interactive bridges share one
        # project root, so containment alone says only "some worktree bridge here" —
        # ownership is what names which, and without it they collapse into one bucket.
        wt_candidates = next(
            (ids for wt_dir, ids in worktree_dirs if cwd.is_relative_to(wt_dir)), []
        )
        # Gate this arm ONLY when there is something to disambiguate. With a single
        # worktree bridge at this root, containment already names the owner, so gating adds
        # no information — and it would cost real coverage: where the process tree can't be
        # walked (AccessDenied under hidepid / a hardened container) `owned_pids` yields
        # just the bridge pid, and every worktree session would flip to EXTERNAL. Those
        # sessions would then show up NOWHERE, since the external-session grouping joins on
        # the exact project path and a worktree cwd never matches it (#1076 regression).
        # With N bridges sharing the subtree, ownership is the only thing that can name the
        # owner, so there the gate is what makes the attribution honest (#1020 A3).
        #
        # `unproven_may_claim` because EXTERNAL is not a visible answer here: the external
        # grouping joins on the exact project path, which a worktree cwd never matches, so
        # refusing to attribute makes the session render NOWHERE rather than surfacing it
        # as unmanaged. A pid-less candidate — a bridge that just spawned — may therefore
        # claim a worker even beside a keyed sibling, which is who a brand-new worker in
        # this subtree most likely belongs to. A keyed candidate that doesn't own the pid
        # still never claims it, so two running worktree bridges can't absorb each other's
        # sessions.
        wt_id = (
            _select_owner(wt_candidates, s.pid, owned_pids_by_instance, unproven_may_claim=True)
            if len(wt_candidates) > 1
            else (wt_candidates[0] if wt_candidates else None)
        )
        if wt_id is not None:
            s.parent_instance = wt_id
            s.attribution = Attribution.TRACKED
            continue
        hosted_id = hosted_by_cwd.get(cwd)
        if hosted_id is not None:
            s.parent_instance = hosted_id
            s.attribution = Attribution.HOSTED
            continue
        s.attribution = Attribution.EXTERNAL
    return sessions
