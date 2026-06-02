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
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from .config import CloneConfig
from .discovery import is_valid_project_name

# git progress lines are separated by CR (in-place updates) or LF.
_PROGRESS_SPLIT = re.compile(rb"[\r\n]")

_log = logging.getLogger("clauster.provisioning")

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
    """The project name fails validation or would escape projects_root."""


class TargetExists(ProvisionError):
    """A directory with the requested name already exists under projects_root."""


class InvalidCloneUrl(ProvisionError):
    """The clone URL is malformed or uses a disallowed scheme."""


class BlockedCloneHost(ProvisionError):
    """The clone URL resolves to a blocked (private/loopback/link-local) address."""


class CloneFailed(ProvisionError):
    """The ``git clone`` itself failed (network, auth, or size-cap exceeded)."""


class GitUnavailable(ProvisionError):
    """The ``git`` binary could not be found on PATH."""


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
    """Create an empty project directory under projects_root (optionally ``git init``)."""
    target = _safe_target(projects_root, name)
    try:
        target.mkdir(parents=False, exist_ok=False)
    except FileExistsError as exc:  # lost the exists()->mkdir race
        raise TargetExists(f"a directory named {name!r} already exists") from exc
    if git_init:
        # Resolve then exec (mirrors clone_project): Windows CreateProcess only
        # auto-appends .exe, so a bare "git" would skip a git.cmd shim that
        # shutil.which resolves — running a different/again-resolved binary.
        resolved_git = shutil.which("git")
        if resolved_git is None:
            raise GitUnavailable("git is not installed on the host")
        try:
            subprocess.run(
                [resolved_git, "init", "--quiet", str(target)],
                check=True,
                capture_output=True,
                timeout=30,
                env=_git_env(),
            )
        except FileNotFoundError as exc:  # race: git vanished after which()
            raise GitUnavailable("git is not installed on the host") from exc
        except subprocess.CalledProcessError as exc:
            shutil.rmtree(target, ignore_errors=True)
            raise CloneFailed(f"git init failed: {_stderr_tail(exc.stderr)}") from exc
    return target


# ----- clone ------------------------------------------------------------


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, cfg: CloneConfig) -> bool:
    # Normalize IPv4-mapped IPv6 (::ffff:a.b.c.d) to its IPv4 so the loopback/
    # private/CGNAT classification below can't be bypassed via the mapped form.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # Always blocked, even with allow_private_hosts — never a legit remote clone
    # target and the SSRF crown jewels.
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
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
    addresses = {str(info[4][0]) for info in infos}
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
    """Build an environment that locks git down for untrusted clone URLs.

    Only https/ssh transports, no credential prompts (fail fast instead of hanging),
    no system config, no interactive auth.

    Residual (accepted, single-user): an ssh:// clone offers the host's own ssh
    identity/agent to the target server, so pasting an attacker ssh URL is a
    credential-probing vector. Operators who don't need it can set
    clone.allowed_schemes: [https] to drop ssh entirely.
    """
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
            except OSError as exc:
                # Unstattable file undercounts the size cap; log so it's not a
                # silent gap (the cap is a post-clone best-effort check anyway).
                _log.warning("size walk could not stat %s: %s", fp, exc)
    return total / (1024 * 1024)


def clone_project(
    projects_root: Path,
    name: str,
    url: str,
    *,
    cfg: CloneConfig,
    shallow: bool = False,
    git_binary: str = "git",
    progress_cb: Callable[[str], None] | None = None,
) -> Path:
    """Clone ``url`` into a new project under projects_root, enforcing the clone guards.

    Validates the scheme, blocks private/loopback hosts (unless opted in), and
    enforces the post-clone size cap. When ``progress_cb`` is given, each git
    progress line (``Receiving objects: 42% …``) is forwarded to it live.
    """
    if not cfg.enabled:
        raise ProvisionError("clone is disabled in config (clone.enabled = false)")
    target = _safe_target(projects_root, name)
    validate_clone_url(url, cfg)
    # Resolve once and exec the resolved path: CreateProcess on Windows only
    # auto-appends .exe (never .cmd/.bat), so a bare name would skip a .cmd shim
    # that shutil.which (PATHEXT-aware) does find. Resolving also avoids a TOCTOU
    # between this check and the spawn.
    resolved_git = shutil.which(git_binary)
    if resolved_git is None:
        raise GitUnavailable("git is not installed on the host")

    # Clone into a unique dot-prefixed temp dir (skipped by discovery; the random
    # suffix avoids a fixed/predictable path and lets two clones of the same name
    # run without trampling each other) then rename, so a failed/partial clone
    # never appears as a project.
    tmp = projects_root / f".{name}.{uuid.uuid4().hex[:8]}.clone-tmp"

    cmd = [
        resolved_git,
        "-c",
        "protocol.file.allow=never",
        "-c",
        "credential.helper=",
        "clone",
        "--progress",  # force progress to stderr even though it isn't a TTY
        "--no-tags",
        "--no-recurse-submodules",
    ]
    if shallow:
        cmd += ["--depth", "1"]
    cmd += ["--", url, str(tmp)]

    try:
        _run_clone_streaming(cmd, cfg.timeout_seconds, progress_cb)
    except CloneFailed:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

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


def _run_clone_streaming(
    cmd: list[str],
    timeout_seconds: int,
    progress_cb: Callable[[str], None] | None,
) -> None:
    """Run ``git clone`` (``cmd``), forwarding stderr progress lines to ``progress_cb``.

    Raise ``CloneFailed`` on a non-zero exit or if the clone exceeds
    ``timeout_seconds`` — a watchdog terminates the process so a stalled transfer
    can never hang the worker thread.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=_git_env(),
    )
    timed_out = threading.Event()

    def _terminate() -> None:
        timed_out.set()
        proc.terminate()

    watchdog = threading.Timer(timeout_seconds, _terminate)
    watchdog.start()
    tail: deque[str] = deque(maxlen=20)
    buf = b""
    try:
        stderr = proc.stderr
        if stderr is None:  # pragma: no cover - unreachable with stderr=PIPE, narrows type
            raise CloneFailed("git clone produced no stderr stream")
        fd = stderr.fileno()
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                break  # EOF — git closed stderr
            buf += chunk
            *complete, buf = _PROGRESS_SPLIT.split(buf)
            for raw in complete:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                tail.append(line)
                if progress_cb is not None:
                    progress_cb(line)
        proc.wait()
    finally:
        watchdog.cancel()

    if timed_out.is_set():
        raise CloneFailed(f"clone timed out after {timeout_seconds}s")
    if proc.returncode != 0:
        raise CloneFailed(f"git clone failed: {_stderr_tail(' / '.join(tail))}")


def _stderr_tail(stderr: bytes | str | None, limit: int = 400) -> str:
    if stderr is None:
        return ""
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr
    return text.strip()[-limit:]
