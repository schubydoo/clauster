from __future__ import annotations

import json
import os

from clauster.discovery import (
    _DiscoveryCache,
    discover_projects,
    discover_projects_cached,
    invalidate_discovery_cache,
    is_valid_project_name,
    trust_state_for,
)
from clauster.models import TrustState


def test_is_valid_project_name():
    assert is_valid_project_name("dockerize2")
    assert is_valid_project_name("my-project_1")
    assert not is_valid_project_name("bad name!")
    assert not is_valid_project_name("../escape")
    assert not is_valid_project_name("")
    assert not is_valid_project_name("x" * 65)
    # Windows reserved device names (#914): pass the char regex but can't be dirs on Windows.
    assert not is_valid_project_name("CON")
    assert not is_valid_project_name("nul")  # case-insensitive
    assert not is_valid_project_name("COM1")
    assert not is_valid_project_name("LPT9")
    assert is_valid_project_name("console")  # a reserved name as a substring is fine


def test_discovers_directories_and_badges(projects_root, tmp_path):
    # No trust file -> everything untrusted
    projects = discover_projects(projects_root, claude_json=tmp_path / "nope.json")
    names = [p.name for p in projects]
    assert names == ["alpha", "beta", "gamma"]  # sorted, dotdir + bad-name skipped

    by_name = {p.name: p for p in projects}
    assert by_name["alpha"].is_git_repo is True
    assert by_name["alpha"].has_claude_md is False
    assert by_name["beta"].has_claude_md is True
    assert by_name["gamma"].is_git_repo is False


def test_trust_inherits_down_tree(tmp_path):
    claude_json = tmp_path / ".claude.json"
    root = tmp_path / "projects"
    child = root / "svc"
    child.mkdir(parents=True)
    claude_json.write_text(json.dumps({"projects": {str(root): {"hasTrustDialogAccepted": True}}}))
    trusted = {root}
    assert trust_state_for(child, trusted) is TrustState.TRUSTED


def test_untrusted_when_no_ancestor(tmp_path):
    target = tmp_path / "projects" / "svc"
    target.mkdir(parents=True)
    assert trust_state_for(target, set()) is TrustState.UNTRUSTED


def test_git_repo_does_not_inherit_ancestor_trust(tmp_path):
    # A git repo (has .git) is NOT trusted by an ancestor grant — it needs its own key
    # (Claude Code 2.1.232+, #1224). Contrast test_trust_inherits_down_tree, whose child
    # is a plain dir and DOES inherit.
    root = tmp_path / "projects"
    repo = root / "repo"
    (repo / ".git").mkdir(parents=True)
    assert trust_state_for(repo, {root}) is TrustState.UNTRUSTED  # ancestor grant ignored
    assert trust_state_for(repo, {repo}) is TrustState.TRUSTED  # own key trusts


def test_discover_git_repo_needs_own_trust_key(projects_root, tmp_path):
    # Claude Code 2.1.232+ (#1224): a git repo is trusted only by its OWN key; a
    # non-repo dir still inherits from a trusted ancestor. Trusting only the
    # projects_root therefore trusts the non-git projects (beta, gamma) but NOT the
    # git repo (alpha) — matching what the CLI honors at spawn, so the badge can't
    # fail open (green over a dir the CLI rejects).
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps({"projects": {str(projects_root): {"hasTrustDialogAccepted": True}}})
    )
    by_name = {p.name: p for p in discover_projects(projects_root, claude_json=claude_json)}
    assert by_name["alpha"].trust_state is TrustState.UNTRUSTED  # git repo, no own key
    assert by_name["beta"].trust_state is TrustState.TRUSTED  # non-repo inherits
    assert by_name["gamma"].trust_state is TrustState.TRUSTED  # non-repo inherits


def test_discover_git_repo_trusted_by_own_key(projects_root, tmp_path):
    # Granting the git repo its OWN key trusts it — exactly what the CLI now requires (#1224).
    claude_json = tmp_path / ".claude.json"
    alpha = projects_root / "alpha"
    claude_json.write_text(
        json.dumps({"projects": {str(alpha): {"hasTrustDialogAccepted": True}}})
    )
    by_name = {p.name: p for p in discover_projects(projects_root, claude_json=claude_json)}
    assert by_name["alpha"].trust_state is TrustState.TRUSTED


def test_load_trusted_paths_non_utf8_returns_empty(tmp_path):
    # A non-UTF-8 claude.json raises UnicodeDecodeError (a ValueError) on read;
    # it must degrade to "nothing trusted" like any other malformed file.
    from clauster.discovery import _load_trusted_paths

    claude_json = tmp_path / ".claude.json"
    claude_json.write_bytes(b"\xff\xfe\x00not utf-8")
    assert _load_trusted_paths(claude_json) == set()


def test_load_trusted_paths_non_dict_json_returns_empty(tmp_path):
    # A valid-JSON-but-non-dict top level (`[]`, `"x"`, `5`) has no `.get`, and a
    # deeply-nested doc raises RecursionError before json returns a value on every
    # supported interpreter (the message changed from "maximum recursion depth exceeded"
    # on <=3.13 to "Stack overflow" on 3.14+, but not the type); both must degrade to
    # "nothing trusted" like any other malformed claude.json (the #122 never-raise
    # contract) instead of raising AttributeError/RecursionError.
    from clauster.discovery import _load_trusted_paths

    claude_json = tmp_path / ".claude.json"
    for content in ("[]", '"x"', "5", "[" * 100_000):
        claude_json.write_text(content, encoding="utf-8")
        assert _load_trusted_paths(claude_json) == set()


# ----- discovery cache (TTL + mtime invalidation) -----------------------


def test_cache_hit_skips_rescan(projects_root, tmp_path, monkeypatch):
    # Within the TTL with an unchanged root + claude.json, a second call is served from
    # the cache and does NOT re-run the underlying filesystem scan — the point of the cache.
    import clauster.discovery as disc

    calls = {"n": 0}
    real_scan = disc.discover_projects

    def _counting_scan(*a, **k):
        calls["n"] += 1
        return real_scan(*a, **k)

    monkeypatch.setattr(disc, "discover_projects", _counting_scan)
    cache = _DiscoveryCache(ttl_seconds=60.0)
    claude_json = tmp_path / ".claude.json"
    first = cache.get(projects_root, claude_json)
    second = cache.get(projects_root, claude_json)
    assert calls["n"] == 1  # second call hit the cache — no rescan
    assert [p.name for p in second] == [p.name for p in first]


def test_cache_returns_copies_so_caller_mutation_never_reaches_cache(projects_root, tmp_path):
    # The cache must hand back COPIES: an app-layer mutation on a returned Project
    # (e.g. stamping allow_bypass_permissions) must not leak into the cached snapshot,
    # so a later reader still sees the pure-filesystem model default.
    cache = _DiscoveryCache(ttl_seconds=60.0)
    claude_json = tmp_path / ".claude.json"
    first = cache.get(projects_root, claude_json)
    assert first[0].allow_bypass_permissions is False  # model default
    first[0].allow_bypass_permissions = True  # caller mutates its own copy
    second = cache.get(projects_root, claude_json)  # same cache entry (TTL fresh)
    assert second[0] is not first[0]  # a fresh copy, not the mutated object
    assert second[0].allow_bypass_permissions is False  # cache stayed pristine


def test_cache_ttl_expiry_rescans(projects_root, tmp_path):
    # Once the TTL lapses, the next call rescans and returns a fresh, equal list.
    cache = _DiscoveryCache(ttl_seconds=0.0)  # everything is immediately stale
    claude_json = tmp_path / ".claude.json"
    first = cache.get(projects_root, claude_json)
    second = cache.get(projects_root, claude_json)
    assert second is not first  # TTL=0 forces a rescan every call
    assert [p.name for p in second] == [p.name for p in first]


def test_cache_invalidates_on_new_project_dir(projects_root, tmp_path):
    # Adding a project changes projects_root's mtime -> the cache must rescan and
    # surface the new directory rather than serving the stale list.
    cache = _DiscoveryCache(ttl_seconds=60.0)
    claude_json = tmp_path / ".claude.json"
    before = {p.name for p in cache.get(projects_root, claude_json)}
    assert "delta" not in before
    (projects_root / "delta").mkdir()
    # Force a distinct directory mtime even on a coarse-resolution filesystem.
    future = os.stat(projects_root).st_mtime + 10
    os.utime(projects_root, (future, future))
    after = {p.name for p in cache.get(projects_root, claude_json)}
    assert "delta" in after


def test_cache_invalidates_on_claude_json_trust_change(projects_root, tmp_path):
    # A trust write touches ~/.claude.json -> its mtime moves -> the cache rescans and
    # reflects the new trust state (a bridge/trust state must not be served stale).
    cache = _DiscoveryCache(ttl_seconds=60.0)
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{}")
    before = cache.get(projects_root, claude_json)
    assert all(p.trust_state is TrustState.UNTRUSTED for p in before)
    # Grant each discovered project its OWN key so the flip holds under the per-repo
    # rule (#1224) — a git repo would not be trusted by a projects_root grant alone.
    claude_json.write_text(
        json.dumps({"projects": {str(p.path): {"hasTrustDialogAccepted": True} for p in before}})
    )
    future = os.stat(claude_json).st_mtime + 10
    os.utime(claude_json, (future, future))
    after = cache.get(projects_root, claude_json)
    assert all(p.trust_state is TrustState.TRUSTED for p in after)


def test_module_cache_invalidate_forces_rescan(projects_root, tmp_path, monkeypatch):
    # The module-level helper + explicit invalidation: a repeat call hits the cache
    # (no rescan), and invalidate_discovery_cache() drops it so the next call rescans.
    import clauster.discovery as disc

    calls = {"n": 0}
    real_scan = disc.discover_projects

    def _counting_scan(*a, **k):
        calls["n"] += 1
        return real_scan(*a, **k)

    monkeypatch.setattr(disc, "discover_projects", _counting_scan)
    claude_json = tmp_path / ".claude.json"
    invalidate_discovery_cache()
    discover_projects_cached(projects_root, claude_json)
    discover_projects_cached(projects_root, claude_json)
    assert calls["n"] == 1  # second call served from cache
    invalidate_discovery_cache()
    discover_projects_cached(projects_root, claude_json)
    assert calls["n"] == 2  # invalidation forced a fresh scan


def test_load_trusted_paths_skips_non_dict_and_untrusted_entries(tmp_path):
    # discovery.py 58->57: per-entry hardening — a non-dict projects value and a dict
    # whose hasTrustDialogAccepted isn't exactly True are both skipped; only the
    # explicit True entry lands in the trusted set (trust must never be inferred).
    from pathlib import Path

    from clauster.discovery import _load_trusted_paths

    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps(
            {
                "projects": {
                    "/p/str-entry": "not-a-dict",
                    "/p/declined": {"hasTrustDialogAccepted": False},
                    "/p/absent-flag": {},
                    "/p/trusted": {"hasTrustDialogAccepted": True},
                }
            }
        )
    )
    assert _load_trusted_paths(claude_json) == {Path("/p/trusted")}
