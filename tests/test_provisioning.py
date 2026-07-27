"""Project create + clone (spec §5 + §11) — module + route coverage. No network:
URL validation uses IP literals / monkeypatched getaddrinfo, and clones use a fake
`git` stub that records the transport env Clauster passed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from clauster import provisioning
from clauster.app import create_app
from clauster.config import CloneConfig, load_config
from clauster.provisioning import (
    BlockedCloneHost,
    CloneFailed,
    GitUnavailable,
    InvalidCloneUrl,
    InvalidProjectName,
    ProvisionError,
    TargetExists,
    _git_env,
    _ip_blocked,
    clone_project,
    create_project,
    validate_clone_url,
)
from conftest import needs_symlink

FAKE_GIT = Path(__file__).resolve().parent / "fixtures" / "fake_git"

# Windows can't exec/resolve the extensionless `git` stub; a `.cmd` wrapper sits
# beside it and is what shutil.which/subprocess must target on Windows.
_WIN_STUB_SUFFIX = ".cmd" if sys.platform == "win32" else ""


def test_git_env_scrubs_clauster_secret(monkeypatch):
    """A clone runs an attacker-controllable repo, so the git env must omit Clauster secrets."""
    monkeypatch.setenv("CLAUSTER_SESSION_SECRET", "must-not-leak-to-a-clone")
    monkeypatch.setenv("CLAUSTER_AUTH_PASSWORD_HASH", "$argon2id$leak")
    env = _git_env()
    assert "CLAUSTER_SESSION_SECRET" not in env
    assert "CLAUSTER_AUTH_PASSWORD_HASH" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"  # the git lockdown overlay still applies


# ----- create -----------------------------------------------------------


def test_create_project_makes_dir(tmp_path):
    p = create_project(tmp_path, "fresh")
    assert p == tmp_path / "fresh" and p.is_dir()


def test_create_project_git_init(tmp_path):
    p = create_project(tmp_path, "withgit", git_init=True)
    assert (p / ".git").is_dir()  # real git on PATH


def test_create_duplicate_rejected(tmp_path):
    (tmp_path / "dup").mkdir()
    with pytest.raises(TargetExists):
        create_project(tmp_path, "dup")


@pytest.mark.parametrize("bad", ["../etc", "a/b", "..", "foo bar", "", "x" * 65])
def test_create_bad_name_rejected(tmp_path, bad):
    with pytest.raises(InvalidProjectName):
        create_project(tmp_path, bad)


def test_create_mkdir_race_is_target_exists(tmp_path, monkeypatch):
    # exists() passes but mkdir loses a race -> FileExistsError -> TargetExists.
    monkeypatch.setattr(
        "clauster.provisioning.Path.mkdir",
        lambda self, *a, **k: (_ for _ in ()).throw(FileExistsError()),
    )
    with pytest.raises(TargetExists):
        create_project(tmp_path, "racy")


def test_create_git_init_missing_git(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "clauster.provisioning.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()),
    )
    with pytest.raises(GitUnavailable):
        create_project(tmp_path, "g", git_init=True)


def test_create_git_init_failure_cleans_up(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise subprocess.CalledProcessError(1, "git", stderr=b"init blew up")

    monkeypatch.setattr("clauster.provisioning.subprocess.run", boom)
    with pytest.raises(CloneFailed):
        create_project(tmp_path, "g", git_init=True)
    assert not (tmp_path / "g").exists()  # rolled back


# ----- url validation ---------------------------------------------------


def _cfg(**kw) -> CloneConfig:
    return CloneConfig(**kw)


def test_url_public_ip_allowed():
    validate_clone_url("https://8.8.8.8/o/r.git", _cfg())  # public, no DNS needed


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/r.git",  # http not allowed
        "file:///etc/passwd",  # file scheme
        "git://example.com/r.git",  # git scheme
        "ext::sh -c whoami",  # transport-helper RCE vector
        "/local/path",  # no scheme
    ],
)
def test_url_bad_scheme_rejected(url):
    with pytest.raises(InvalidCloneUrl):
        validate_clone_url(url, _cfg())


@pytest.mark.parametrize(
    "url",
    [
        "///[\n",  # malformed IPv6 literal -> urlsplit ValueError (found by fuzzing)
        "https://[::1",  # unterminated IPv6 bracket
        "https://example.com:99999/r.git",  # out-of-range port -> .port ValueError
    ],
)
def test_url_malformed_rejected_not_crash(url):
    # A malformed URL must surface as InvalidCloneUrl (-> 422), never a raw
    # ValueError from urlsplit()/.port (-> 500).
    with pytest.raises(InvalidCloneUrl):
        validate_clone_url(url, _cfg())


def test_url_private_blocked_by_default():
    with pytest.raises(BlockedCloneHost):
        validate_clone_url("https://10.0.0.1/r.git", _cfg())


def test_url_private_allowed_when_opted_in():
    validate_clone_url("https://10.0.0.1/r.git", _cfg(allow_private_hosts=True))


def test_url_loopback_blocked_even_when_private_allowed():
    with pytest.raises(BlockedCloneHost):
        validate_clone_url("https://127.0.0.1/r.git", _cfg(allow_private_hosts=True))


def test_url_link_local_blocked():
    with pytest.raises(BlockedCloneHost):
        validate_clone_url("https://169.254.1.1/r.git", _cfg(allow_private_hosts=True))


def test_url_cidr_allowlist():
    cfg = _cfg(allowed_private_cidrs=["192.168.1.0/24"])  # allow_private_hosts stays False
    validate_clone_url("https://192.168.1.5/r.git", cfg)
    with pytest.raises(BlockedCloneHost):
        validate_clone_url("https://192.168.2.5/r.git", cfg)  # outside the CIDR


@pytest.mark.parametrize("ip", ["100.64.0.1", "198.18.0.1", "192.0.0.1"])
def test_url_extra_private_ranges_blocked_by_default(ip):
    # CGNAT/benchmark/IETF ranges that ipaddress.is_private doesn't flag (HIGH-1).
    with pytest.raises(BlockedCloneHost):
        validate_clone_url(f"https://{ip}/r.git", _cfg())


def test_url_cgnat_allowed_when_opted_in():
    # Tailscale 100.x is reachable only when the operator opts in.
    validate_clone_url("https://100.64.0.1/r.git", _cfg(allow_private_hosts=True))


def test_url_ipv4_mapped_loopback_blocked():
    # ::ffff:127.0.0.1 must not bypass the loopback block via the mapped form.
    with pytest.raises(BlockedCloneHost):
        validate_clone_url("https://[::ffff:127.0.0.1]/r.git", _cfg(allow_private_hosts=True))


def test_url_ipv4_mapped_cgnat_blocked_by_default():
    with pytest.raises(BlockedCloneHost):
        validate_clone_url("https://[::ffff:100.64.0.1]/r.git", _cfg())


def test_clone_rename_conflict_is_target_exists(tmp_path, monkeypatch):
    # A target appearing between _safe_target and os.rename -> TargetExists, tmp cleaned.
    monkeypatch.setenv("FAKE_GIT_MODE", "ok")

    def boom(src, dst):
        Path(dst).mkdir()  # simulate the TOCTOU: the target appeared between _safe_target and now
        raise FileExistsError(dst)

    monkeypatch.setattr("clauster.provisioning.os.rename", boom)
    with pytest.raises(TargetExists):
        clone_project(
            tmp_path,
            "proj",
            "https://10.0.0.1/r.git",
            cfg=_cfg(allow_private_hosts=True),
            git_binary=_gitbin(),
        )
    leftover = [p for p in tmp_path.glob(".proj.*clone-tmp")]
    assert leftover == []  # temp dir cleaned up on the failed rename


def test_clone_rename_transient_failure_is_clone_failed(tmp_path, monkeypatch):
    # A rename failure where the target does NOT exist (e.g. a Windows sharing violation while
    # an AV scanner holds the fresh tree) is a transient finalize failure — surfaced as
    # CloneFailed, never mislabeled "already exists" (#914).
    monkeypatch.setenv("FAKE_GIT_MODE", "ok")
    monkeypatch.setattr(
        "clauster.provisioning.os.rename",
        lambda s, d: (_ for _ in ()).throw(PermissionError("sharing violation")),
    )
    with pytest.raises(CloneFailed):
        clone_project(
            tmp_path,
            "proj",
            "https://10.0.0.1/r.git",
            cfg=_cfg(allow_private_hosts=True),
            git_binary=_gitbin(),
        )
    assert [p for p in tmp_path.glob(".proj.*clone-tmp")] == []  # temp cleaned


def test_url_no_host_rejected():
    with pytest.raises(InvalidCloneUrl):
        validate_clone_url("https:///just/a/path.git", _cfg())


def test_url_unresolvable_host_rejected():
    # .invalid is reserved (RFC 6761) -> guaranteed NXDOMAIN -> gaierror, offline-safe.
    with pytest.raises(InvalidCloneUrl):
        validate_clone_url("https://nonexistent.invalid./r.git", _cfg())


def test_malformed_cidr_rejected_at_config_load():
    # A garbage CIDR is now rejected when CloneConfig is built (fail-fast), rather
    # than silently never matching — so it can never widen the SSRF allowlist.
    with pytest.raises(ValueError):
        _cfg(allowed_private_cidrs=["garbage"])


def test_url_hostname_resolves_to_private_blocked(monkeypatch):
    monkeypatch.setattr(
        "clauster.provisioning.socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("192.168.0.9", 443))],
    )
    with pytest.raises(BlockedCloneHost):
        validate_clone_url("https://internal.example/r.git", _cfg())


def test_url_mixed_public_and_private_records_blocked(monkeypatch):
    # DNS-rebinding / multi-A defense: a host that returns one public AND one
    # private address must be blocked (we block if ANY record is private), not
    # passed because the first happens to be public.
    monkeypatch.setattr(
        "clauster.provisioning.socket.getaddrinfo",
        lambda *a, **k: [
            (2, 1, 6, "", ("8.8.8.8", 443)),
            (2, 1, 6, "", ("10.0.0.1", 443)),
        ],
    )
    with pytest.raises(BlockedCloneHost):
        validate_clone_url("https://rebind.example/r.git", _cfg())


def test_ip_blocked_skips_malformed_cidr_entry():
    # The config loader rejects a malformed CIDR (test_malformed_cidr_rejected_at_
    # config_load), so reach _ip_blocked directly via model_construct to bypass the
    # field_validator. A garbage allowlist entry is `continue`-skipped, so a private
    # target stays BLOCKED — a malformed CIDR can never widen access.
    import ipaddress

    cfg = CloneConfig.model_construct(
        allow_private_hosts=False, allowed_private_cidrs=["not-a-cidr"]
    )
    assert _ip_blocked(ipaddress.ip_address("10.0.0.1"), cfg) is True


def test_clone_url_host_resolving_to_no_addresses_rejected(monkeypatch):
    # getaddrinfo succeeds but yields zero addresses -> the host can't be classified,
    # so the URL is rejected as InvalidCloneUrl rather than silently passing.
    monkeypatch.setattr(
        "clauster.provisioning.socket.getaddrinfo",
        lambda *a, **k: [],
    )
    with pytest.raises(InvalidCloneUrl, match="resolved to no addresses"):
        validate_clone_url("https://empty.example/r.git", _cfg())


def test_clone_url_blocks_zoned_ipv6_link_local_from_resolver(monkeypatch):
    # A real resolver returns IPv6 link-local addresses with a zone id appended
    # (e.g. "fe80::1%eth0"), delivered in a getaddrinfo IPv6 4-tuple (host, port,
    # flowinfo, scope_id) — an SSRF-relevant edge the IPv4 private-host tests don't
    # reach. validate_clone_url strips the "%zone" suffix before ipaddress.ip_address()
    # (provisioning.py); this asserts a resolver-returned zoned link-local address is
    # classified and BLOCKED end-to-end (not slipped through, not surfaced as a raw
    # ValueError -> 500). The test exercises the full resolve->classify path; treat the
    # production strip as load-bearing (don't remove it on the strength of this test).
    monkeypatch.setattr(
        "clauster.provisioning.socket.getaddrinfo",
        lambda *a, **k: [(10, 1, 6, "", ("fe80::1%eth0", 443, 0, 2))],
    )
    with pytest.raises(BlockedCloneHost):
        validate_clone_url("https://zoned.example/r.git", _cfg())


# ----- clone (fake git) -------------------------------------------------


def _gitbin() -> str:
    return str(FAKE_GIT / f"git{_WIN_STUB_SUFFIX}")


def test_clone_success_and_transport_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_GIT_MODE", "ok")
    dest = clone_project(
        tmp_path,
        "proj",
        "https://10.0.0.1/r.git",
        cfg=_cfg(allow_private_hosts=True),
        git_binary=_gitbin(),
    )
    assert (dest / "README.md").exists()
    assert not (tmp_path / ".proj.clone-tmp").exists()  # tmp renamed away
    rec = json.loads((dest / ".fakegit.json").read_text())
    assert rec["GIT_ALLOW_PROTOCOL"] == "https:ssh"  # transport lockdown reached git
    assert "--no-recurse-submodules" in rec["argv"]


def test_clone_with_claude_files(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_GIT_MODE", "with_claude")
    dest = clone_project(
        tmp_path,
        "proj",
        "https://10.0.0.1/r.git",
        cfg=_cfg(allow_private_hosts=True),
        git_binary=_gitbin(),
    )
    assert (dest / "CLAUDE.md").exists() and (dest / ".claude").is_dir()


def test_clone_shallow_passes_depth(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_GIT_MODE", "ok")
    dest = clone_project(
        tmp_path,
        "proj",
        "https://10.0.0.1/r.git",
        cfg=_cfg(allow_private_hosts=True),
        git_binary=_gitbin(),
        shallow=True,
    )
    argv = json.loads((dest / ".fakegit.json").read_text())["argv"]
    assert "--depth" in argv and argv[argv.index("--depth") + 1] == "1"


def test_clone_failure_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_GIT_MODE", "fail")
    with pytest.raises(CloneFailed):
        clone_project(
            tmp_path,
            "proj",
            "https://10.0.0.1/r.git",
            cfg=_cfg(allow_private_hosts=True),
            git_binary=_gitbin(),
        )
    assert not (tmp_path / "proj").exists()
    assert not (tmp_path / ".proj.clone-tmp").exists()


def test_clone_timeout(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_GIT_MODE", "slow")
    monkeypatch.setenv("FAKE_GIT_SLEEP", "3")
    with pytest.raises(CloneFailed):
        clone_project(
            tmp_path,
            "proj",
            "https://10.0.0.1/r.git",
            cfg=_cfg(allow_private_hosts=True, timeout_seconds=1),
            git_binary=_gitbin(),
        )
    assert not (tmp_path / "proj").exists()


def test_clone_timeout_reaps_the_tree_on_windows(tmp_path, monkeypatch):
    """The clone watchdog reaps git's DESCENDANTS on Windows, not just the pid we hold.

    `terminate()` there kills only its argument, and a `git clone` helper (or, in this
    suite, the fake-git `.cmd` shim's python child) inherits our stderr pipe — so a
    surviving helper keeps that pipe open and the read loop runs for as long as the CHILD
    wants rather than the timeout we configured. Measured on the CI matrix at dc69e21:
    Linux honoured the 1s timeout (1.01s), Windows paid the stub's full 3s sleep (3.12s).
    """
    monkeypatch.setenv("FAKE_GIT_MODE", "slow")
    monkeypatch.setenv("FAKE_GIT_SLEEP", "2")  # just over the 1s timeout: these stub the reap
    monkeypatch.setattr(provisioning.procutil, "is_windows", lambda: True)
    killed: list[int] = []
    monkeypatch.setattr(provisioning.procutil, "force_kill_tree", lambda pid: killed.append(pid))
    with pytest.raises(CloneFailed):
        clone_project(
            tmp_path,
            "proj",
            "https://10.0.0.1/r.git",
            cfg=_cfg(allow_private_hosts=True, timeout_seconds=1),
            git_binary=_gitbin(),
        )
    assert killed, "the watchdog must reap the clone's process tree on Windows"


def test_clone_timeout_does_not_tree_kill_on_posix(tmp_path, monkeypatch):
    """POSIX keeps the plain `terminate()`: the pid we hold IS git, and SIGTERM is graceful.

    A tree kill here would trade a clean shutdown for SIGKILL against descendants to fix a
    problem POSIX does not have — `terminate()` already signals the process we spawned.
    """
    monkeypatch.setenv("FAKE_GIT_MODE", "slow")
    monkeypatch.setenv("FAKE_GIT_SLEEP", "2")  # just over the 1s timeout: these stub the reap
    monkeypatch.setattr(provisioning.procutil, "is_windows", lambda: False)
    killed: list[int] = []
    monkeypatch.setattr(provisioning.procutil, "force_kill_tree", lambda pid: killed.append(pid))
    with pytest.raises(CloneFailed):
        clone_project(
            tmp_path,
            "proj",
            "https://10.0.0.1/r.git",
            cfg=_cfg(allow_private_hosts=True, timeout_seconds=1),
            git_binary=_gitbin(),
        )
    assert killed == [], "POSIX teardown must not force-kill the tree"


def test_clone_timeout_survives_a_tree_kill_that_raises(tmp_path, monkeypatch):
    """A failing reap still leaves the plain `terminate()` to run.

    `_terminate` is a `threading.Timer` callback — nobody observes a raise from it, so an
    unguarded failure would silently skip the terminate and let the clone run unbounded,
    which is the exact hang the watchdog exists to prevent.
    """
    monkeypatch.setenv("FAKE_GIT_MODE", "slow")
    monkeypatch.setenv("FAKE_GIT_SLEEP", "2")  # just over the 1s timeout: these stub the reap
    monkeypatch.setattr(provisioning.procutil, "is_windows", lambda: True)

    def _boom(pid: int) -> None:
        raise OSError("psutil could not walk the tree")

    monkeypatch.setattr(provisioning.procutil, "force_kill_tree", _boom)
    with pytest.raises(CloneFailed):  # still terminated, still bounded
        clone_project(
            tmp_path,
            "proj",
            "https://10.0.0.1/r.git",
            cfg=_cfg(allow_private_hosts=True, timeout_seconds=1),
            git_binary=_gitbin(),
        )
    assert not (tmp_path / "proj").exists()


def test_clone_size_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_GIT_MODE", "big")
    monkeypatch.setenv("FAKE_GIT_BIG_MB", "2")
    with pytest.raises(CloneFailed):
        clone_project(
            tmp_path,
            "proj",
            "https://10.0.0.1/r.git",
            cfg=_cfg(allow_private_hosts=True, max_mb=1),
            git_binary=_gitbin(),
        )
    assert not (tmp_path / "proj").exists()


def test_clone_blocked_host_before_git(tmp_path):
    with pytest.raises(BlockedCloneHost):
        clone_project(
            tmp_path, "proj", "https://10.0.0.1/r.git", cfg=_cfg(), git_binary=_gitbin()
        )  # default: private blocked


def test_clone_disabled_raises(tmp_path):
    with pytest.raises(ProvisionError):
        clone_project(
            tmp_path,
            "x",
            "https://8.8.8.8/r.git",
            cfg=_cfg(enabled=False),
            git_binary=_gitbin(),
        )


def test_clone_git_binary_unavailable(tmp_path):
    with pytest.raises(GitUnavailable):
        clone_project(
            tmp_path,
            "x",
            "https://10.0.0.1/r.git",
            cfg=_cfg(allow_private_hosts=True),
            git_binary="definitely-not-git-xyz",
        )


def test_clone_streams_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_GIT_MODE", "progress")
    seen: list[str] = []
    dest = clone_project(
        tmp_path,
        "proj",
        "https://10.0.0.1/r.git",
        cfg=_cfg(allow_private_hosts=True),
        git_binary=_gitbin(),
        progress_cb=seen.append,
    )
    assert (dest / "README.md").exists()  # clone still completes
    assert any("Receiving objects" in line for line in seen)
    assert any("100%" in line for line in seen)


# ----- routes -----------------------------------------------------------


def _client(write_config, extra: str = "") -> TestClient:
    return TestClient(create_app(load_config(write_config(extra))))


def test_route_create_project(write_config):
    client = _client(write_config)
    resp = client.post("/api/projects", json={"name": "brandnew"})
    assert resp.status_code == 201
    assert resp.json()["name"] == "brandnew"
    assert "brandnew" in [p["name"] for p in client.get("/api/projects").json()]


def test_route_create_duplicate_409(write_config):
    client = _client(write_config)
    assert client.post("/api/projects", json={"name": "alpha"}).status_code == 409  # fixture dir


def test_route_create_bad_name_422(write_config):
    client = _client(write_config)
    assert client.post("/api/projects", json={"name": "../evil"}).status_code == 422


def test_route_clone_disabled_403(write_config):
    client = _client(write_config, "clone:\n  enabled: false\n")
    resp = client.post("/api/projects/clone", json={"name": "x", "url": "https://8.8.8.8/r.git"})
    assert resp.status_code == 403


def test_route_clone_blocked_host_403(write_config):
    client = _client(write_config)  # default: private blocked
    resp = client.post("/api/projects/clone", json={"name": "x", "url": "https://10.0.0.1/r.git"})
    assert resp.status_code == 403


def test_route_clone_bad_url_422(write_config):
    client = _client(write_config)
    resp = client.post("/api/projects/clone", json={"name": "x", "url": "file:///etc/passwd"})
    assert resp.status_code == 422


@pytest.mark.parametrize("url", ["", "   ", None])
def test_route_clone_missing_url_friendly_422(write_config, url):
    # #749: an empty/whitespace/absent Git URL gets the friendly required-URL message,
    # not a raw URL-format error (the client guards this too, but the API stays the
    # fail-closed backstop).
    client = _client(write_config)
    resp = client.post("/api/projects/clone", json={"name": "x", "url": url})
    assert resp.status_code == 422
    assert resp.json()["detail"] == "A Git URL is required to clone."


def _drain_clone_ws(client, job_id: str) -> list[dict]:
    """Watch a clone job's WS to completion, returning every frame received."""
    events: list[dict] = []
    with client.websocket_connect(f"/ws/clone-progress/{job_id}") as ws:
        while True:
            evt = ws.receive_json()
            events.append(evt)
            if evt["type"] == "done":
                break
    return events


def test_route_clone_async_success(write_config, monkeypatch):
    monkeypatch.setenv("PATH", str(FAKE_GIT) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_GIT_MODE", "with_claude")
    # `with`: keep the lifespan portal alive so the background clone task runs
    # (and so the progress WS can observe it) across both requests.
    with _client(write_config, "clone:\n  allow_private_hosts: true\n") as client:
        resp = client.post(
            "/api/projects/clone", json={"name": "cloned", "url": "https://10.0.0.1/r.git"}
        )
        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        events = _drain_clone_ws(client, job_id)
        assert events[-1] == {"type": "done", "status": "done", "error": None}
        # the project landed (cloned dir discovered) once the job finished
        names = [p["name"] for p in client.get("/api/projects").json()]
        assert "cloned" in names


def test_route_clone_async_failure_via_ws(write_config, monkeypatch):
    monkeypatch.setenv("PATH", str(FAKE_GIT) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_GIT_MODE", "fail")
    with _client(write_config, "clone:\n  allow_private_hosts: true\n") as client:
        resp = client.post(
            "/api/projects/clone", json={"name": "x", "url": "https://10.0.0.1/r.git"}
        )
        assert resp.status_code == 202
        events = _drain_clone_ws(client, resp.json()["job_id"])
        terminal = events[-1]
        assert terminal["type"] == "done" and terminal["status"] == "error"
        assert "git clone failed" in terminal["error"]
        # nothing landed
        assert "x" not in [p["name"] for p in client.get("/api/projects").json()]


def test_route_clone_ws_reconnect_after_done(write_config, monkeypatch):
    monkeypatch.setenv("PATH", str(FAKE_GIT) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_GIT_MODE", "ok")
    with _client(write_config, "clone:\n  allow_private_hosts: true\n") as client:
        resp = client.post(
            "/api/projects/clone", json={"name": "redo", "url": "https://10.0.0.1/r.git"}
        )
        job_id = resp.json()["job_id"]
        _drain_clone_ws(client, job_id)  # watch it finish
        # reconnect while still in the registry: the finished job replays its outcome
        events = _drain_clone_ws(client, job_id)
        assert events == [{"type": "done", "status": "done", "error": None}]


def test_route_clone_progress_ws_unknown_job_closes(write_config):
    client = _client(write_config, "clone:\n  allow_private_hosts: true\n")
    with pytest.raises(WebSocketDisconnect):  # server closes (1008) on an unknown job
        with client.websocket_connect("/ws/clone-progress/deadbeef") as ws:
            ws.receive_json()


def test_route_create_git_init_unavailable_503(write_config, monkeypatch):
    monkeypatch.setattr(
        "clauster.provisioning.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()),
    )
    client = _client(write_config)
    resp = client.post("/api/projects", json={"name": "gg", "git_init": True})
    assert resp.status_code == 503


# ----- audited coverage gaps (2026-07 audit) -----------------------------


@needs_symlink
def test_create_project_symlink_escaping_root_rejected(tmp_path):
    # provisioning.py 86-87: a validly-NAMED entry that RESOLVES outside projects_root
    # (a symlink planted inside it) must be refused by the resolve()-based guard —
    # the regex alone can't see through symlinks, this is the defense-in-depth layer.
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "escapee").symlink_to(outside)
    with pytest.raises(InvalidProjectName, match="escapes projects_root"):
        create_project(root, "escapee")


def test_create_project_git_init_without_git_binary(tmp_path, monkeypatch):
    # provisioning.py 105-106: git_init requested but no git on PATH -> the explicit
    # GitUnavailable (a 503 upstream), never a confusing FileNotFoundError.
    monkeypatch.setattr("clauster.provisioning.shutil.which", lambda name: None)
    with pytest.raises(GitUnavailable, match="not installed"):
        create_project(tmp_path, "nogit", git_init=True)


def test_dir_size_walk_warns_on_unstattable_file(tmp_path, caplog, monkeypatch):
    # provisioning.py 231-234: an unstattable file undercounts the post-clone size cap;
    # the walk must log the gap (never silent) and keep counting the rest. Induce the
    # OSError by monkeypatching stat for one file so the handler runs on every OS.
    from clauster.provisioning import _dir_size_mb

    d = tmp_path / "clone"
    d.mkdir()
    (d / "ok.bin").write_bytes(b"x" * 1024)  # stattable -> counted
    (d / "locked.bin").write_bytes(b"y" * 4096)  # unstattable -> undercounted
    real_stat = Path.stat

    def boom(self, *args, **kwargs):
        if self.name == "locked.bin":
            raise OSError("simulated unstattable file")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", boom)
    with caplog.at_level("WARNING", logger="clauster.provisioning"):
        size = _dir_size_mb(d)
    assert any("could not stat" in r.message for r in caplog.records)  # surfaced, not silent
    assert 0 < size < 0.01  # the stattable file still counted; the walk continued


def test_stderr_tail_none_is_empty():
    # provisioning.py 374-375: a clone that produced no stderr at all reports an empty
    # tail, so CloneFailed messages never render a "None".
    from clauster.provisioning import _stderr_tail

    assert _stderr_tail(None) == ""
