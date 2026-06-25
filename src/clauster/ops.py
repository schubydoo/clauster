"""Operational CLIs: doctor / backup / restore / migrate / install-service (spec §"v0.2").

Kept separate from ``__main__`` so the logic is unit-testable without argparse. None
of these touch the network or spawn bridges; they inspect config + manage state_dir.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal
from xml.sax.saxutils import escape as _xml_escape

from . import claude_cli, environments, procutil
from .config import ClausterConfig, _missing_enforced_auth, load_config
from .discovery import Project, _load_trusted_paths, trust_state_for
from .state import CURRENT_SCHEMA, StateStore

# ----- doctor -----------------------------------------------------------

# Ops-local diagnostic vocabulary; not promoted to models/config (it's doctor-local).
CheckStatus = Literal["ok", "warn", "fail"]

OK: CheckStatus = "ok"
WARN: CheckStatus = "warn"
FAIL: CheckStatus = "fail"


@dataclass
class Check:
    """One doctor diagnostic result: a named check, its status, and a detail line."""

    name: str
    status: CheckStatus
    detail: str


def _version_ge(have: str, want: str) -> bool:
    """Compare dotted versions numerically (2.1.156 >= 2.1.145). Missing/odd parts -> 0."""

    def parse(v: str) -> tuple[int, ...]:
        parts = []
        for seg in v.split("."):
            digits = "".join(c for c in seg if c.isdigit())
            parts.append(int(digits) if digits else 0)
        return tuple(parts)

    a, b = parse(have), parse(want)
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) >= b + (0,) * (n - len(b))


def run_doctor(
    config_path: str | None = None, *, check_port: bool = True
) -> tuple[list[Check], bool]:
    """Return (checks, ok). ok is False if any check FAILed. Resilient to a broken config.

    ``check_port`` gates the listen-port availability probe. It's a meaningful
    *pre-launch* check for the ``clauster doctor`` CLI (is the port free to bind?),
    but a guaranteed false positive when the **running server** calls this for the
    dashboard preflight panel — the port is in use *by that very server*, so it would
    always warn "already in use (Clauster already running?)". The server passes
    ``check_port=False`` to omit it (the port isn't a bridge prerequisite anyway —
    bridges don't bind it).
    """
    checks: list[Check] = []

    try:
        config: ClausterConfig | None = load_config(config_path)
        checks.append(Check("config", OK, f"loaded {config.source_path}"))
    except FileNotFoundError as exc:
        checks.append(Check("config", FAIL, f"no config found: {exc}"))
        config = None
    except ValueError as exc:
        checks.append(Check("config", FAIL, f"invalid config: {exc}"))
        config = None

    if config is None:
        return checks, all(c.status != FAIL for c in checks)

    # claude binary + version
    try:
        version = claude_cli.claude_version(config.claude.binary)
        if _version_ge(version, config.claude.min_version):
            checks.append(Check("claude", OK, f"{version} (>= {config.claude.min_version})"))
        else:
            checks.append(
                Check("claude", FAIL, f"{version} < required {config.claude.min_version}")
            )
    except claude_cli.ClaudeNotFound as exc:
        checks.append(Check("claude", FAIL, str(exc)))
    except Exception as exc:  # noqa: BLE001 - surface any probe failure
        checks.append(Check("claude", FAIL, f"version probe failed: {exc}"))

    # claude logged in — a spawned bridge inherits the operator's `claude` login, so a
    # present-but-logged-out CLI starts a bridge that can't authenticate.
    checks.append(_check_claude_login())

    # projects_root (config validation already requires it exists+readable, but surface it)
    pr = config.projects_root
    checks.append(Check("projects_root", OK if pr.is_dir() else FAIL, str(pr)))

    # state_dir writable
    checks.append(_check_state_dir_writable(config.state_dir))

    # git (needed for create --git-init and clone)
    if shutil.which("git"):
        checks.append(Check("git", OK, shutil.which("git") or "git"))
    else:
        checks.append(
            Check("git", WARN, "git not on PATH — create --git-init / clone unavailable")
        )

    # auth sanity
    checks.append(_check_auth(config))

    # workspace trust (informational)
    trusted = trust_state_for(pr, _load_trusted_paths(claude_cli_json()))
    checks.append(
        Check(
            "workspace-trust",
            OK if trusted.value == "trusted" else WARN,
            f"projects_root is {trusted.value}",
        )
    )

    # port (warn-only). Skipped for the running server's dashboard preflight, where the
    # port is in use *by this very server* and the warning is always a false positive.
    if check_port:
        checks.append(_check_port(config.host, config.port))

    # source-checkout freshness (only for editable/from-source installs)
    fresh = _check_repo_freshness()
    if fresh is not None:
        checks.append(fresh)

    # systemd cgroup-reap guard (only when an installed Clauster unit is loaded)
    killmode = _check_systemd_killmode()
    if killmode is not None:
        checks.append(killmode)

    return checks, all(c.status != FAIL for c in checks)


def project_preflight_checks(project: Project) -> list[Check]:
    """Per-project spawn-readiness checks (trust + git), mirroring the doctor shape.

    Complements the system-wide ``run_doctor`` panel with the two preconditions that
    are specific to *one* project's bridge launch: workspace **trust** (a standard
    spawn raises ``NotTrusted`` without it) and whether the directory is a **git
    repo** (required for the ``worktree`` spawn mode). Both are advisory (``WARN``,
    never ``FAIL``) — each is recoverable from the UI (trust-on-start; pick a
    non-worktree mode) — so the panel informs without blocking. Pure/read-only:
    derives from the already-discovered ``Project`` (no extra subprocess or fs scan).
    """
    trusted = project.trust_state.value == "trusted"
    return [
        Check(
            "trust",
            OK if trusted else WARN,
            "workspace trusted"
            if trusted
            else f"directory is {project.trust_state.value} — Trust before starting "
            "(or use trust-on-start)",
        ),
        Check(
            "git",
            OK if project.is_git_repo else WARN,
            "git repository"
            if project.is_git_repo
            else "not a git repository — the worktree spawn mode is unavailable",
        ),
    ]


def _check_repo_freshness(repo: Path | None = None) -> Check | None:
    """Report whether a from-source checkout is behind its upstream.

    If Clauster runs from a git checkout (editable / from-source install), this tells
    the operator whether to ``git pull`` + restart. Returns None for non-git installs
    (PyPI/Docker) — there's nothing to upgrade in place. Read-only and offline: it
    compares against the last-fetched upstream ref, never the network, so doctor stays
    fast and works without connectivity.
    """
    repo = repo or Path(__file__).resolve().parents[2]  # src/clauster/ops.py -> repo root
    if not (repo / ".git").exists():
        return None  # installed package, not a source checkout
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-list", "--left-right", "--count", "@{upstream}...HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            env=procutil.child_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("version", WARN, f"source checkout; git freshness check failed: {exc}")
    if out.returncode != 0:
        # No upstream tracking (detached HEAD, local-only branch) — not an error.
        return Check("version", OK, "source checkout (no upstream tracking configured)")
    try:
        behind, ahead = (int(n) for n in out.stdout.split())
    except ValueError:
        return Check("version", OK, "source checkout")
    if behind:
        plural = "s" if behind != 1 else ""
        return Check(
            "version",
            WARN,
            f"{behind} commit{plural} behind upstream (as of last fetch) — "
            "git pull && restart to upgrade",
        )
    suffix = f" (+{ahead} local)" if ahead else ""
    return Check("version", OK, f"up to date with upstream{suffix}")


def _check_systemd_killmode(unit: str = "clauster.service") -> Check | None:
    """Warn when an installed systemd unit would reap live pty bridges on restart.

    A pty (true-resume) bridge is a detached child living in Clauster's service
    cgroup. With systemd's default ``KillMode=control-group``, a ``systemctl
    restart``/``stop`` kills the whole cgroup — taking live bridges down with the
    service, even though Clauster's own shutdown leaves them running and
    :meth:`SessionRunner.rediscover` would otherwise reattach them on startup.
    ``KillMode=process`` signals only the main process, so the bridges survive.

    Asks systemd directly (``systemctl show``) so the answer reflects the loaded
    unit, not a guessed file path. Returns None when there's nothing to advise on:
    no ``systemctl`` (non-systemd host — macOS/Windows/Docker) or no loaded Clauster
    unit under the conventional ``clauster.service`` name.
    """
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return None  # not a systemd host — nothing to advise
    try:
        out = subprocess.run(
            [systemctl, "show", unit, "--property=LoadState,KillMode"],
            capture_output=True,
            text=True,
            timeout=5,
            env=procutil.child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None  # systemctl present but unusable (no manager, container) — stay quiet
    if out.returncode != 0:
        return None
    props = dict(line.split("=", 1) for line in out.stdout.splitlines() if "=" in line)
    if props.get("LoadState") != "loaded":
        return None  # no installed Clauster unit to advise on
    kill_mode = props.get("KillMode", "control-group")
    if kill_mode in ("process", "none"):
        return Check("systemd", OK, f"{unit}: KillMode={kill_mode} (bridges survive a restart)")
    return Check(
        "systemd",
        WARN,
        f"{unit}: KillMode={kill_mode} reaps live pty bridges on a restart. To fix, install a "
        "KillMode=process unit and reload: `sudo clauster install-service systemd --write` then "
        f"`sudo systemctl daemon-reload && sudo systemctl restart {unit}` — that one restart "
        "still reaps the current pty bridges, but later restarts won't.",
    )


def claude_cli_json() -> Path:
    """Return the path to the user's ``~/.claude.json``."""
    return Path("~/.claude.json").expanduser()


def _check_state_dir_writable(state_dir: Path) -> Check:
    # Read-only diagnostic: NEVER create the tree (that would mask a misconfigured
    # path). Probe an existing dir; otherwise check the nearest existing ancestor.
    sd = state_dir.expanduser()
    if sd.exists():
        try:
            probe = sd / ".doctor-write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return Check("state_dir", OK, f"{sd} (writable)")
        except OSError as exc:
            return Check("state_dir", FAIL, f"{sd} not writable: {exc}")
    ancestor = sd
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    if os.access(ancestor, os.W_OK):
        return Check("state_dir", OK, f"{sd} (absent; creatable under {ancestor})")
    return Check("state_dir", FAIL, f"{sd} can't be created: {ancestor} not writable")


def _check_claude_login(creds_path: Path | None = None) -> Check:
    """Check that the ``claude`` CLI actually has usable credentials.

    A spawned bridge inherits the operator's ``claude`` login; without it the bridge
    starts but can't authenticate (the dogfood "bridge runs but is dead" failure mode).
    The other checks confirm the binary is present and new enough — this confirms it's
    logged in. WARN, never FAIL: an ``ANTHROPIC_API_KEY`` in the environment is a valid
    alternative, and a missing/expired token is recoverable with ``claude`` (not a broken
    install). Reads only the credentials file's presence + token field — never the token.
    """
    creds = (creds_path or environments.CREDENTIALS_PATH).expanduser()
    has_token = False
    state = "missing"  # missing | present | bad_json
    bad_json = ""
    try:
        data = json.loads(creds.read_text(encoding="utf-8"))
        # Valid JSON that isn't an object (null, a number, a list) must not crash doctor.
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        oauth = oauth if isinstance(oauth, dict) else {}
        has_token = bool(oauth.get("accessToken"))
        state = "present"
    except (FileNotFoundError, OSError):
        state = "missing"  # no credentials file yet
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        # A non-UTF-8 creds file (UnicodeDecodeError, a ValueError) is as unusable
        # as invalid JSON — report it the same way instead of crashing doctor.
        state, bad_json = "bad_json", str(exc)

    if has_token:
        return Check("claude-login", OK, "logged in (claude credentials present)")
    if os.environ.get("ANTHROPIC_API_KEY"):
        return Check("claude-login", OK, "ANTHROPIC_API_KEY set in environment")
    if state == "bad_json":
        return Check("claude-login", WARN, f"{creds} is not valid JSON: {bad_json}")
    if state == "present":
        return Check(
            "claude-login", WARN, f"logged out (no access token in {creds}) — re-run `claude`"
        )
    return Check(
        "claude-login",
        WARN,
        f"not logged in ({creds} missing) — run `claude` to authenticate; "
        "bridges inherit the operator's login",
    )


def _check_auth(config: ClausterConfig) -> Check:
    a = config.auth
    if a.password_required and not a.password_hash:
        return Check("auth", FAIL, "password_required but no password_hash set")
    # Same "is auth actually enforced?" rule the config validator uses, so doctor never
    # calls a config consistent that the validator would refuse to start.
    if _missing_enforced_auth(config.host, a):
        if a.allow_unauthenticated_network:
            return Check("auth", WARN, "bound non-loopback with auth explicitly disabled")
        return Check("auth", FAIL, f"non-loopback host {config.host} without enforced auth")
    return Check("auth", OK, "configuration consistent")


def _check_port(host: str, port: int) -> Check:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            in_use = s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        in_use = False
    if in_use:
        return Check("port", WARN, f"{port} already in use (Clauster already running?)")
    return Check("port", OK, f"{port} free")


# ----- backup / restore -------------------------------------------------

_MANIFEST = "manifest.json"


def make_backup(config: ClausterConfig, out: Path, *, now: datetime | None = None) -> Path:
    """tar.gz of state_dir + the resolved config file. Returns the written path.

    NB: the config file carries the argon2 password *hash* (not plaintext) — the
    backup is sensitive, store it accordingly.
    """
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    out = out.expanduser()
    if out.is_dir():
        out = out / f"clauster-backup-{stamp}.tar.gz"
    state_dir = config.state_dir.expanduser()
    cfg_path = config.source_path

    manifest = {
        "created": stamp,
        "schema_version": CURRENT_SCHEMA,
        "has_state_dir": state_dir.is_dir(),
        "has_config": cfg_path is not None and Path(cfg_path).is_file(),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    # The archive embeds the argon2 password hash and the session secret, so
    # create it 0600 up front — never momentarily world-readable on a shared host.
    # (0600 has no group/other bits for the umask to clear; a no-op on Windows.)
    os.close(os.open(out, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600))
    with tarfile.open(out, "w:gz") as tar:
        if state_dir.is_dir():
            tar.add(state_dir, arcname="state")
        if cfg_path and Path(cfg_path).is_file():
            tar.add(cfg_path, arcname=f"config/{Path(cfg_path).name}")
        info = tarfile.TarInfo(_MANIFEST)
        data = json.dumps(manifest, indent=2).encode()
        info.size = len(data)
        import io

        tar.addfile(info, io.BytesIO(data))
    return out


def _safe_extract_tar(backup: Path, dest: Path) -> None:
    """Extract a tar.gz into dest, validating each member's path ourselves.

    Deliberately avoids ``extractall``: we reject absolute paths and ``..`` traversal,
    skip symlinks/hardlinks/devices entirely, and write file contents to a path proven
    to stay under dest — so a malicious archive (CVE-2007-4559) cannot escape.
    """
    dest_resolved = dest.resolve()
    with tarfile.open(backup, "r:gz") as tar:
        for member in tar.getmembers():
            if not (member.isfile() or member.isdir()):
                continue  # drop symlinks/hardlinks/devices/fifos
            rel = PurePosixPath(member.name)
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"unsafe path in archive: {member.name!r}")
            target = (dest / rel).resolve()
            if target != dest_resolved and dest_resolved not in target.parents:
                raise ValueError(f"archive member escapes destination: {member.name!r}")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            with src, open(target, "wb") as fh:
                shutil.copyfileobj(src, fh)


def _atomic_replace_state(src_state: Path, state_dir: Path) -> int:
    """Replace ``state_dir`` with the contents of ``src_state`` atomically.

    Stages a full copy in a sibling temp dir on ``state_dir``'s own filesystem,
    then swaps it in with directory renames (move the old aside, move the staged
    copy into place, drop the old). So a mid-copy failure never leaves a
    half-applied ``state_dir``, and a forced restore is replace-not-merge — stale
    files absent from the backup don't linger. Returns the file count restored.
    """
    state_dir = state_dir.expanduser()
    parent = state_dir.parent
    parent.mkdir(parents=True, exist_ok=True)

    # Stage on the destination's filesystem so the swap below is a rename, not a
    # cross-device copy (TemporaryDirectory may live on a different mount).
    staged = Path(tempfile.mkdtemp(prefix=f".{state_dir.name}.restore-", dir=parent))
    try:
        count = 0
        for item in src_state.rglob("*"):
            target = staged / item.relative_to(src_state)
            if item.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
                count += 1
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise

    # Swap. The aside name is unique (derived from the staged temp name), so the
    # renames work on POSIX and Windows alike and never replace an existing dir.
    old_aside = parent / f"{staged.name}.old"
    moved_old = state_dir.exists()
    if moved_old:
        os.replace(state_dir, old_aside)
    try:
        os.replace(staged, state_dir)
    except BaseException:
        if moved_old:
            os.replace(old_aside, state_dir)  # roll back to the pre-restore state
        shutil.rmtree(staged, ignore_errors=True)
        raise
    if moved_old:
        shutil.rmtree(old_aside, ignore_errors=True)
    return count


def restore_backup(
    backup: Path,
    *,
    state_dir: Path,
    config_out: Path | None = None,
    force: bool = False,
) -> dict:
    """Restore state (and optionally config) from a backup.

    Extraction is hardened against path traversal / absolute paths / symlink escape
    (see _safe_extract_tar).
    """
    backup = backup.expanduser()
    state_dir = state_dir.expanduser()
    if config_out is not None:
        config_out = config_out.expanduser()
    if not backup.is_file():
        raise FileNotFoundError(f"backup not found: {backup}")
    # Validate BOTH destinations up front so a config_out conflict can't leave a
    # half-applied restore (state already copied, then we abort on the config).
    if state_dir.exists() and any(state_dir.iterdir()) and not force:
        raise FileExistsError(f"{state_dir} is not empty; pass force=True to overwrite")
    if config_out is not None and config_out.exists() and not force:
        raise FileExistsError(f"{config_out} exists; pass force=True to overwrite")

    restored = {"state_files": 0, "config": None}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _safe_extract_tar(backup, tmp)

        src_state = tmp / "state"
        if src_state.is_dir():
            # Atomic replace-not-merge: build the new state on the destination's
            # filesystem and swap it in, so a forced restore can't leave a mix of
            # old + new files (or stale files the backup no longer contains).
            restored["state_files"] = _atomic_replace_state(src_state, state_dir)

        src_cfg_dir = tmp / "config"
        if config_out is not None and src_cfg_dir.is_dir():
            cfgs = [p for p in src_cfg_dir.iterdir() if p.is_file()]
            if cfgs:
                config_out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cfgs[0], config_out)
                restored["config"] = str(config_out)
    return restored


# ----- migrate ----------------------------------------------------------


def migrate_state(config: ClausterConfig) -> dict:
    """Force state.json to the current schema, then re-save canonical.

    ``StateStore.load`` migrates (and ``.bak``s) older schemas on read; clauster.yml
    is additive-only, so this just confirms it still validates.
    """
    store = StateStore(config.state_dir)
    data = store.load()  # migrates older schema in place (taking a .bak)
    store.save(data)  # rewrite canonical at CURRENT_SCHEMA
    return {"schema_version": CURRENT_SCHEMA, "instances": len(data)}


# ----- install-service --------------------------------------------------

_SERVICE_KINDS = ("systemd", "launchd", "windows")


def default_service_path(kind: str) -> Path:
    """Return the conventional install path for a service definition of *kind*.

    Used by ``install-service --write`` (and referenced in the doctor remediation)
    so a unit lands where the platform's service manager looks for it. systemd's
    path is system-wide and needs privileges to write; the caller surfaces that.
    """
    if kind == "systemd":
        return Path("/etc/systemd/system/clauster.service")
    if kind == "launchd":
        return Path("~/Library/LaunchAgents/org.clauster.daemon.plist").expanduser()
    if kind == "windows":
        return Path.cwd() / "install-clauster-service.bat"
    raise ValueError(f"unknown service kind {kind!r}; expected one of {_SERVICE_KINDS}")


def _bat_quote_safe(value: str) -> str:
    """Return ``value`` for embedding in a ``"..."`` batch token, or raise on a quote.

    A ``"`` is not a legal Windows path character, so its presence in a
    python/workdir/config path is a sign of malformed input rather than a path we
    should try to escape. Reject it loudly so a stray quote can never break out of
    the surrounding ``"%s"`` and inject extra batch tokens.
    """
    if '"' in value:
        raise ValueError(f"value contains an illegal double-quote for a batch path: {value!r}")
    return value


def _service_launch_command(python: str | None) -> tuple[str, list[str]]:
    """Return the (executable, leading-args) a service unit uses to launch clauster.

    An explicit *python* is treated as an interpreter and invoked with the
    ``-m clauster`` module form (the back-compatible behavior). When *python* is
    None, detect how clauster is actually installed and drop that prefix for
    anything that is already the clauster entry point:

    - a frozen / standalone binary (PyInstaller) — ``sys.executable`` *is* clauster;
    - otherwise a ``clauster`` console script resolvable on PATH (uv tool / pipx /
      pip) — a stable entry point that needs no interpreter path. This trusts the
      caller's PATH; only an *absolute* resolution is accepted, since a relative
      ``ExecStart``/``ProgramArguments[0]`` would fail the service manager's
      absolute-path requirement rather than launch.

    Only a bare interpreter (dev / ``python -m clauster``) keeps ``-m clauster``.
    Prepending it to the clauster binary produced ``clauster -m clauster run``,
    which clauster's own argparse rejects, so a frozen install's unit never started.
    """
    if python is not None:
        return python, ["-m", "clauster"]
    if getattr(sys, "frozen", False):
        # PyInstaller: sys.executable is the clauster binary, not an interpreter.
        return sys.executable, []
    script = shutil.which("clauster")
    if script and os.path.isabs(script):
        return script, []
    return sys.executable, ["-m", "clauster"]


# Standard system bin dirs systemd/launchd would otherwise leave as the whole PATH.
# Prepending ~/.local/bin is what lets a spawned bridge resolve uv-installed tools.
_SYSTEM_PATH_DIRS = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin"


def _home_for_user(user: str | None) -> str:
    """Best-effort home dir for the service's run-as user (for ``~/.local/bin``).

    Returns a plain string, not a ``Path``: these are POSIX-only systemd/launchd
    units, and ``Path``'s string form is platform-dependent (it would emit
    backslashes if this ever ran on Windows). Resolve a named *user* via the passwd
    db when it exists on the rendering host (the normal case — install-service runs
    on the target host). Fall back to ``<root>/<user>`` if the lookup fails
    (rendering for a user not yet created, or a non-POSIX host). With no *user*, the
    service runs as the invoking user, so use their home.
    """
    if user:
        try:
            import pwd

            return pwd.getpwnam(user).pw_dir
        except (KeyError, ImportError, OSError):
            # Lookup miss (user not yet created / non-POSIX host): guess the home root
            # from the rendering host's platform — macOS homes live under /Users.
            base = "/Users" if sys.platform == "darwin" else "/home"
            return f"{base}/{user}"
    return os.path.expanduser("~")


def _service_path(user: str | None) -> str:
    """Build the PATH baked into a generated unit.

    The run-as user's ``~/.local/bin`` comes first, then the standard system bin
    dirs. Bridges inherit this via ``child_env()``.
    """
    return f"{_home_for_user(user)}/.local/bin:{_SYSTEM_PATH_DIRS}"


def render_service_unit(
    kind: str,
    *,
    python: str | None = None,
    config_path: str | None = None,
    workdir: str | None = None,
    user: str | None = None,
) -> str:
    """Render a service definition (systemd/launchd/windows) for the given kind."""
    if kind not in _SERVICE_KINDS:
        raise ValueError(f"unknown service kind {kind!r}; expected one of {_SERVICE_KINDS}")
    exe, launch = _service_launch_command(python)
    cfg = config_path or "/etc/clauster/clauster.yml"
    wd = workdir or str(Path(cfg).expanduser().parent)
    cmd_args = [*launch, "run", "-c", cfg]

    if kind == "systemd":
        u = f"User={user}\n" if user else ""
        args = " ".join(cmd_args)
        path = _service_path(user)
        return (
            "[Unit]\n"
            "Description=Clauster — browser-driven claude remote-control manager\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"{u}"
            f"ExecStart={exe} {args}\n"
            f"WorkingDirectory={wd}\n"
            f"Environment=CLAUSTER_CONFIG={cfg}\n"
            "# systemd gives a service a minimal PATH; clauster propagates this PATH to\n"
            "# every bridge it spawns (procutil.child_env), so set it here. ~/.local/bin\n"
            "# covers uv-installed tools; for shell-managed toolchains (nvm/pyenv/cargo/go)\n"
            "# extend it via claude.path_append / claude.env in clauster.yml.\n"
            f"Environment=PATH={path}\n"
            "Restart=on-failure\n"
            "RestartSec=5\n"
            "# Signal only the main process on stop/restart. Detached pty (true-resume)\n"
            "# bridges live in this service's cgroup; the default KillMode=control-group\n"
            "# would reap them on every restart. KillMode=process leaves them running so\n"
            "# Clauster reattaches them on startup (see runner.rediscover).\n"
            "KillMode=process\n\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )

    if kind == "launchd":
        # Escape every operator-supplied path before it lands inside an XML
        # <string>: an unescaped & or < in a python/config/workdir path would
        # produce a malformed (or injected) plist. xml.sax.saxutils.escape covers
        # & < > — the full set that is significant in element text.
        arg_xml = "\n".join(f"      <string>{_xml_escape(a)}</string>" for a in [exe, *cmd_args])
        wd_xml = _xml_escape(wd)
        cfg_xml = _xml_escape(cfg)
        # launchd, like systemd, starts the daemon with a minimal PATH; clauster
        # propagates it to every spawned bridge (procutil.child_env). Bake ~/.local/bin
        # in so bridges resolve uv-installed tools; for shell-managed toolchains
        # (nvm/pyenv/cargo/go) extend via claude.path_append / claude.env in clauster.yml.
        path_xml = _xml_escape(_service_path(user))
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n'
            "  <dict>\n"
            "    <key>Label</key><string>org.clauster.daemon</string>\n"
            "    <key>ProgramArguments</key>\n"
            f"    <array>\n{arg_xml}\n    </array>\n"
            "    <key>RunAtLoad</key><true/>\n"
            "    <key>KeepAlive</key><true/>\n"
            f"    <key>WorkingDirectory</key><string>{wd_xml}</string>\n"
            "    <key>EnvironmentVariables</key>\n"
            "    <dict>\n"
            f"      <key>CLAUSTER_CONFIG</key><string>{cfg_xml}</string>\n"
            f"      <key>PATH</key><string>{path_xml}</string>\n"
            "    </dict>\n"
            "  </dict>\n"
            "</plist>\n"
        )

    # windows — nssm script (native Python services are awkward; nssm is the pragmatic path)
    # A double-quote is illegal in a Windows path; rejecting it (rather than escaping)
    # keeps a stray " from breaking out of the "%s" quoting and injecting extra batch
    # tokens. _bat_quote_safe raises on a " so the operator fixes the path.
    exe_q = _bat_quote_safe(exe)
    wd_q = _bat_quote_safe(wd)
    cfg_q = _bat_quote_safe(cfg)
    quoted = " ".join(f'"{_bat_quote_safe(a)}"' for a in cmd_args)
    return (
        "@echo off\n"
        "REM Install Clauster as a Windows service via nssm (https://nssm.cc).\n"
        "REM Run this script from an elevated prompt with nssm on PATH.\n"
        f'nssm install Clauster "{exe_q}" {quoted}\n'
        f'nssm set Clauster AppDirectory "{wd_q}"\n'
        f"nssm set Clauster AppEnvironmentExtra CLAUSTER_CONFIG={cfg_q}\n"
        "nssm set Clauster Start SERVICE_AUTO_START\n"
        "nssm start Clauster\n"
    )
