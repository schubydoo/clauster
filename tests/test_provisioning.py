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
    clone_project,
    create_project,
    validate_clone_url,
)

FAKE_GIT = Path(__file__).resolve().parent / "fixtures" / "fake_git"

# Windows can't exec/resolve the extensionless `git` stub; a `.cmd` wrapper sits
# beside it and is what shutil.which/subprocess must target on Windows.
_WIN_STUB_SUFFIX = ".cmd" if sys.platform == "win32" else ""


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


def test_route_clone_success(write_config, monkeypatch):
    monkeypatch.setenv("PATH", str(FAKE_GIT) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_GIT_MODE", "with_claude")
    client = _client(write_config, "clone:\n  allow_private_hosts: true\n")
    resp = client.post(
        "/api/projects/clone", json={"name": "cloned", "url": "https://10.0.0.1/r.git"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "cloned"
    assert body["has_claude_md"] is True and body["has_claude_dir"] is True


def test_route_clone_failed_502(write_config, monkeypatch):
    monkeypatch.setenv("PATH", str(FAKE_GIT) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_GIT_MODE", "fail")
    client = _client(write_config, "clone:\n  allow_private_hosts: true\n")
    resp = client.post("/api/projects/clone", json={"name": "x", "url": "https://10.0.0.1/r.git"})
    assert resp.status_code == 502


def test_route_create_git_init_unavailable_503(write_config, monkeypatch):
    monkeypatch.setattr(
        "clauster.provisioning.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()),
    )
    client = _client(write_config)
    resp = client.post("/api/projects", json={"name": "gg", "git_init": True})
    assert resp.status_code == 503
