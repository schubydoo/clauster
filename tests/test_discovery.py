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


def test_discover_sets_trust_from_claude_json(projects_root, tmp_path):
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(
        json.dumps({"projects": {str(projects_root): {"hasTrustDialogAccepted": True}}})
    )
    projects = discover_projects(projects_root, claude_json=claude_json)
    assert all(p.trust_state is TrustState.TRUSTED for p in projects)


def test_load_trusted_paths_non_utf8_returns_empty(tmp_path):
    # A non-UTF-8 claude.json raises UnicodeDecodeError (a ValueError) on read;
    # it must degrade to "nothing trusted" like any other malformed file.
    from clauster.discovery import _load_trusted_paths

    claude_json = tmp_path / ".claude.json"
    claude_json.write_bytes(b"\xff\xfe\x00not utf-8")
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
    claude_json.write_text(
        json.dumps({"projects": {str(projects_root): {"hasTrustDialogAccepted": True}}})
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
