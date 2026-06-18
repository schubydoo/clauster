"""Bridge-log retention/rotation policy (#348): logs.retention_* prunes old log sets."""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

from clauster.runner import SessionRunner

_SUFFIXES = (".log", ".raw.log", ".stderr.log", ".keeper.json", ".keeper.log")


def _runner(runner_config) -> SessionRunner:
    config, claude_json = runner_config
    return SessionRunner(config, claude_json=claude_json)


def _make_set(
    log_dir: Path, name: str, ms: int, *, age_days: float = 0.0, size: int = 16
) -> list[Path]:
    """Create one spawn's log set (all siblings) with a backdated mtime; return its files."""
    log_dir.mkdir(parents=True, exist_ok=True)
    mtime = time.time() - age_days * 86400
    paths = []
    for suf in _SUFFIXES:
        p = log_dir / f"{name}-{ms}-1{suf}"
        # The .log carries the bulk (as in real spawns); siblings stay tiny.
        p.write_bytes(b"x" * (size if suf == ".log" else 16))
        os.utime(p, (mtime, mtime))
        paths.append(p)
    return paths


def test_retention_disabled_is_noop(runner_config):
    runner = _runner(runner_config)
    runner._config.logs.retention_max_age_days = 0
    runner._config.logs.retention_max_files = 0
    runner._config.logs.retention_max_total_mb = 0
    old = _make_set(runner._log_dir, "alpha", 1, age_days=999)
    runner._prune_logs()
    assert all(p.exists() for p in old)  # nothing pruned when every dimension is off


def test_retention_by_age_deletes_old_set_keeps_recent(runner_config):
    runner = _runner(runner_config)
    runner._config.logs.retention_max_age_days = 30
    runner._config.logs.retention_max_files = 0
    runner._config.logs.retention_max_total_mb = 0
    old = _make_set(runner._log_dir, "alpha", 1, age_days=40)
    fresh = _make_set(runner._log_dir, "alpha", 2, age_days=1)
    runner._prune_logs()
    assert not any(p.exists() for p in old)  # whole set gone, all siblings
    assert all(p.exists() for p in fresh)


def test_retention_by_count_keeps_newest_n(runner_config):
    runner = _runner(runner_config)
    runner._config.logs.retention_max_age_days = 0
    runner._config.logs.retention_max_files = 2
    runner._config.logs.retention_max_total_mb = 0
    sets = [_make_set(runner._log_dir, "alpha", i, age_days=4 - i) for i in range(4)]
    runner._prune_logs()
    # Newest two (i=2,3) survive; oldest two (i=0,1) pruned.
    assert not any(p.exists() for p in sets[0])
    assert not any(p.exists() for p in sets[1])
    assert all(p.exists() for p in sets[2])
    assert all(p.exists() for p in sets[3])


def test_retention_by_total_size_deletes_oldest_until_under_cap(runner_config):
    runner = _runner(runner_config)
    runner._config.logs.retention_max_age_days = 0
    runner._config.logs.retention_max_files = 0
    runner._config.logs.retention_max_total_mb = 1
    # Three sets ~0.45MB each (~1.35MB total) > 1MB cap -> oldest pruned until under.
    half_mb = 450_000
    sets = [_make_set(runner._log_dir, "alpha", i, age_days=3 - i, size=half_mb) for i in range(3)]
    runner._prune_logs()
    assert not any(p.exists() for p in sets[0])  # oldest dropped
    assert all(p.exists() for p in sets[2])  # newest kept
    total = sum(p.stat().st_size for p in runner._log_dir.iterdir())
    assert total <= 1 * 1024 * 1024


def test_retention_tolerates_unlink_failure(runner_config, monkeypatch):
    # A delete that fails (e.g. a transient FS error) must be swallowed, not crash.
    runner = _runner(runner_config)
    runner._config.logs.retention_max_files = 1
    _make_set(runner._log_dir, "alpha", 1, age_days=5)
    _make_set(runner._log_dir, "alpha", 2, age_days=1)

    def _boom(self: Path, *a, **k):
        raise OSError("nope")

    monkeypatch.setattr(Path, "unlink", _boom)
    runner._prune_logs()  # must not raise


def test_keeper_log_groups_with_its_set(runner_config):
    # The keeper sidecar's `.keeper.log` is a 5th sibling — it must prune WITH its set,
    # not survive in an orphan `<stem>.keeper` bucket.
    runner = _runner(runner_config)
    runner._config.logs.retention_max_age_days = 30
    old = _make_set(runner._log_dir, "alpha", 1, age_days=40)
    keeper_log = next(p for p in old if p.name.endswith(".keeper.log"))
    assert runner._log_set_key(keeper_log.name) == "alpha-1-1"  # same key as the set
    runner._prune_logs()
    assert not keeper_log.exists()  # pruned with the rest of the set


def test_live_instance_set_is_never_pruned(runner_config):
    # A tracked (live) bridge's log set must survive pruning even if old / over the cap.
    runner = _runner(runner_config)
    runner._config.logs.retention_max_age_days = 30
    live = _make_set(runner._log_dir, "alpha", 1, age_days=99)
    log_path = next(p for p in live if p.name.endswith("-1-1.log"))
    runner._instances["alpha"] = SimpleNamespace(
        bridge_debug_log_path=log_path, bridge_raw_log_path=log_path
    )
    runner._prune_logs()
    assert all(p.exists() for p in live)  # protected despite being 99 days old
