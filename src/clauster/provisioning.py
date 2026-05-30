"""Project create + clone (spec §5 + §11 clone+trust chain).

Cloning *into* projects_root is an RCE-sensitive operation: projects_root is a
trusted ancestor, so a freshly cloned repo is trusted the instant it lands and
its CLAUDE.md/hooks/MCP run the moment a bridge starts. This module therefore:

  - never auto-spawns (the caller lands the dir discovered-but-not-started);
  - allowlists only https/ssh and locks git transports (GIT_ALLOW_PROTOCOL, no
    transport helpers, no submodule recursion);
  - blocks clone URLs that resolve to private/loopback/link-local IPs (SSRF),
    unless explicitly opted in via config;
  - confines the target under projects_root with the project-name regex, never
    overwrites, and bounds the clone with a timeout + size cap (clone into a
    dot-prefixed temp dir, then rename — so a failed clone leaves nothing
    discoverable).
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from .config import CloneConfig
from .discovery import is_valid_project_name

# Ranges `ipaddress.is_private` does NOT reliably flag but that must not be a
# clone target by default: CGNAT (100.64/10 — Tailscale/ISP, this very host),
# benchmarking (198.18/15), and IETF protocol assignments (192.0.0/24). Treated
# as private — blocked by default, unlockable via allow_private_hosts/CIDRs.
_EXTRA_PRIVATE_NETS = tuple(
    ipaddress.ip_network(c) for c in ("100.64.0.0/10", "198.18.0.0/15", "192.0.0.0/24")
)


class ProvisionError(RuntimeError):
    """Base for create/clone failures the app maps to HTTP 4xx/5xx."""


class InvalidProjectName(ProvisionError):
    pass


class TargetExists(ProvisionError):
    pass


class InvalidCloneUrl(ProvisionError):
    pass


class BlockedCloneHost(ProvisionError):
    """The clone URL resolves to a blocked (private/loopback/link-local) address."""


class CloneFailed(ProvisionError):
    pass


class GitUnavailable(ProvisionError):
    pass


def _safe_target(projects_root: Path, name: str) -> Path:
    if not is_valid_project_name(name):
        raise InvalidProjectName(f"invalid project name: {name!r}")
    target = projects_root / name
    # Defense in depth: the regex already forbids separators, but confirm the
    # resolved path stays directly under projects_root.
    if target.resolve().parent != projects_root.resolve():
        raise InvalidProjectName(f"project name escapes projects_root: {name!r}")
    if target.exists():
        raise TargetExists(f"a directory named {name!r} already exists")
    return target


def create_project(projects_root: Path, name: str, *, git_init: bool = False) -> Path:
    target = _safe_target(projects_root, name)
    try:
        target.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:  # lost the exists()->mkdir race
        raise TargetExists(f"a directory named {name!r} already exists") from exc
    if git_init:
        try:
            subprocess.run(
                ["git", "init", "--quiet", str(target)],
                check=True,
                capture_output=True,
                timeout=30,
                env=_git_env(),
            )
        except FileNotFoundError as exc:
            raise GitUnavailable("git is not installed on the host") from exc
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(target, ignore_errors=True)
            raise CloneFailed(f"git init failed: {_stderr_tail(exc.stderr)}") from exc
    return target


# ----- clone ------------------------------------------------------------

def _ip_blocked(ip: ipaddress._BaseAddress, cfg: CloneConfig) -> bool:
    # Normalize IPv4-mapped IPv6 (::ffff:a.b.c.d) to its IPv4 so the loopback/
    # private/CGNAT classification below can't be bypassed via the mapped form.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # Always blocked, even with allow_private_hosts — never a legit remote clone
    # target and the SSRF crown jewels.
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    private = ip.is_private or any(ip in net for net in _EXTRA_PRIVATE_NETS)
    if not private:
        return False
    if cfg.allow_private_hosts:
        return False
    for cidr in cfg.allowed_private_cidrs:
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return False
        except ValueError:
            continue  # a malformed CIDR in config never widens access
    return True


def validate_clone_url(url: str, cfg: CloneConfig) -> None:
    """Scheme allowlist + SSRF IP check. Raises on rejection; returns None if allowed.

    Residual DNS-rebind TOCTOU: we resolve here and git re-resolves at clone time,
    so a name that flips public->private between the two reaches the private target.
    Accepted for a single-user loopback tool; note the surface grows if Clauster is
    bound non-loopback (still auth-gated, and private ranges stay blocked unless the
    operator opts in via allow_private_hosts).
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in {s.lower() for s in cfg.allowed_schemes}:
        raise InvalidCloneUrl(
            f"scheme {scheme or '(none)'!r} not allowed; permitted: {cfg.allowed_schemes}"
        )
    host = parts.hostname
    if not host:
        raise InvalidCloneUrl("clone URL has no host")

    # A bare IP literal is checked directly; a name is resolved (all A/AAAA records).
    try:
        infos = socket.getaddrinfo(host, parts.port or None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise InvalidCloneUrl(f"could not resolve host {host!r}: {exc}") from exc
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise InvalidCloneUrl(f"host {host!r} resolved to no addresses")
    for addr in addresses:
        ip = ipaddress.ip_address(addr.split("%")[0])  # strip any zone id
        if _ip_blocked(ip, cfg):
            raise BlockedCloneHost(
                f"host {host!r} resolves to blocked address {ip} "
                "(private/loopback/link-local). Set clone.allow_private_hosts to permit."
            )


def _git_env() -> dict[str, str]:
    """Environment that locks git down: only https/ssh transports, no credential
    prompts (fail fast instead of hanging), no system config, no interactive auth.

    Residual (accepted, single-user): an ssh:// clone offers the host's own ssh
    identity/agent to the target server, so pasting an attacker ssh URL is a
    credential-probing vector. Operators who don't need it can set
    clone.allowed_schemes: [https] to drop ssh entirely."""
    env = dict(os.environ)
    env.update(
        {
            "GIT_ALLOW_PROTOCOL": "https:ssh",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,  # ignore ~/.gitconfig (helpers/insteadOf)
        }
    )
    return env


def _dir_size_mb(path: Path) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = Path(root) / f
            try:
                total += fp.stat(follow_symlinks=False).st_size
            except OSError:
                pass
    return total / (1024 * 1024)


def clone_project(
    projects_root: Path,
    name: str,
    url: str,
    *,
    cfg: CloneConfig,
    shallow: bool = False,
    git_binary: str = "git",
) -> Path:
    if not cfg.enabled:
        raise ProvisionError("clone is disabled in config (clone.enabled = false)")
    target = _safe_target(projects_root, name)
    validate_clone_url(url, cfg)
    if shutil.which(git_binary) is None:
        raise GitUnavailable("git is not installed on the host")

    # Clone into a unique dot-prefixed temp dir (skipped by discovery; the random
    # suffix avoids a fixed/predictable path and lets two clones of the same name
    # run without trampling each other) then rename, so a failed/partial clone
    # never appears as a project.
    tmp = projects_root / f".{name}.{uuid.uuid4().hex[:8]}.clone-tmp"

    cmd = [
        git_binary,
        "-c", "protocol.file.allow=never",
        "-c", "credential.helper=",
        "clone",
        "--no-tags",
        "--no-recurse-submodules",
    ]
    if shallow:
        cmd += ["--depth", "1"]
    cmd += ["--", url, str(tmp)]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=cfg.timeout_seconds,
            env=_git_env(),
        )
    except subprocess.TimeoutExpired as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        raise CloneFailed(f"clone timed out after {cfg.timeout_seconds}s") from exc

    if proc.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise CloneFailed(f"git clone failed: {_stderr_tail(proc.stderr)}")

    # NB: max_mb is a *post-clone* cap (we can't bound the transfer mid-flight
    # without partial-clone filters); the transfer itself is bounded by timeout_seconds.
    if cfg.max_mb and _dir_size_mb(tmp) > cfg.max_mb:
        shutil.rmtree(tmp, ignore_errors=True)
        raise CloneFailed(f"clone exceeds the {cfg.max_mb} MB size cap")

    try:
        os.rename(tmp, target)
    except OSError as exc:  # target appeared between _safe_target and now (TOCTOU)
        shutil.rmtree(tmp, ignore_errors=True)
        raise TargetExists(f"a directory named {name!r} already exists") from exc
    return target


def _stderr_tail(stderr: bytes | str | None, limit: int = 400) -> str:
    if stderr is None:
        return ""
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    return text.strip()[-limit:]
