"""Operational CLIs: doctor / backup / restore / migrate / install-service (spec §"v0.2").

Kept separate from ``__main__`` so the logic is unit-testable without argparse. None
of these touch the network or spawn bridges; they inspect config + manage state_dir.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from . import claude_cli
from .config import ClausterConfig, load_config
from .discovery import _load_trusted_paths, trust_state_for
from .state import CURRENT_SCHEMA, StateStore

# ----- doctor -----------------------------------------------------------

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    name: str
    status: str  # OK | WARN | FAIL
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


def run_doctor(config_path: str | None = None) -> tuple[list[Check], bool]:
    """Return (checks, ok). ok is False if any check FAILed. Resilient to a broken config."""
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

    # port (warn-only: in-use may be this very server)
    checks.append(_check_port(config.host, config.port))

    return checks, all(c.status != FAIL for c in checks)


def claude_cli_json() -> Path:
    return Path("~/.claude.json").expanduser()


def _check_state_dir_writable(state_dir: Path) -> Check:
    # Read-only diagnostic: NEVER create the tree (that would mask a misconfigured
    # path). Probe an existing dir; otherwise check the nearest existing ancestor.
    sd = state_dir.expanduser()
    if sd.exists():
        try:
            probe = sd / ".doctor-write-probe"
            probe.write_text("ok")
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


def _check_auth(config: ClausterConfig) -> Check:
    a = config.auth
    if a.password_required and not a.password_hash:
        return Check("auth", FAIL, "password_required but no password_hash set")
    loopback = config.host in {"127.0.0.1", "::1", "localhost"}
    if not loopback and not (
        a.password_required or a.reverse_proxy.enabled or a.allow_unauthenticated_network
    ):
        return Check("auth", FAIL, f"non-loopback host {config.host} without any auth")
    if not loopback and a.allow_unauthenticated_network:
        return Check("auth", WARN, "bound non-loopback with auth explicitly disabled")
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


def restore_backup(
    backup: Path,
    *,
    state_dir: Path,
    config_out: Path | None = None,
    force: bool = False,
) -> dict:
    """Restore state (and optionally config) from a backup. Extraction is hardened
    against path traversal / absolute paths / symlink escape (see _safe_extract_tar)."""
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
            state_dir.mkdir(parents=True, exist_ok=True)
            for item in src_state.rglob("*"):
                rel = item.relative_to(src_state)
                dest = state_dir / rel
                if item.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest)
                    restored["state_files"] += 1

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
    """Force state.json to the current schema (StateStore.load migrates + .bak's on read),
    then re-save canonical. clauster.yml is additive-only, so just confirm it validates."""
    store = StateStore(config.state_dir)
    data = store.load()  # migrates older schema in place (taking a .bak)
    store.save(data)  # rewrite canonical at CURRENT_SCHEMA
    return {"schema_version": CURRENT_SCHEMA, "instances": len(data)}


# ----- install-service --------------------------------------------------

_SERVICE_KINDS = ("systemd", "launchd", "windows")


def render_service_unit(
    kind: str,
    *,
    python: str | None = None,
    config_path: str | None = None,
    workdir: str | None = None,
    user: str | None = None,
) -> str:
    if kind not in _SERVICE_KINDS:
        raise ValueError(f"unknown service kind {kind!r}; expected one of {_SERVICE_KINDS}")
    python = python or sys.executable
    cfg = config_path or "/etc/clauster/clauster.yml"
    wd = workdir or str(Path(cfg).expanduser().parent)
    cmd_args = ["-m", "clauster", "run", "-c", cfg]

    if kind == "systemd":
        u = f"User={user}\n" if user else ""
        args = " ".join(cmd_args)
        return (
            "[Unit]\n"
            "Description=Clauster — browser-driven claude remote-control manager\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"{u}"
            f"ExecStart={python} {args}\n"
            f"WorkingDirectory={wd}\n"
            f"Environment=CLAUSTER_CONFIG={cfg}\n"
            "Restart=on-failure\n"
            "RestartSec=5\n\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )

    if kind == "launchd":
        arg_xml = "\n".join(f"      <string>{a}</string>" for a in [python, *cmd_args])
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
            f"    <key>WorkingDirectory</key><string>{wd}</string>\n"
            "    <key>EnvironmentVariables</key>\n"
            f"    <dict><key>CLAUSTER_CONFIG</key><string>{cfg}</string></dict>\n"
            "  </dict>\n"
            "</plist>\n"
        )

    # windows — nssm script (native Python services are awkward; nssm is the pragmatic path)
    quoted = " ".join(f'"{a}"' for a in cmd_args)
    return (
        "@echo off\n"
        "REM Install Clauster as a Windows service via nssm (https://nssm.cc).\n"
        "REM Run this script from an elevated prompt with nssm on PATH.\n"
        f'nssm install Clauster "{python}" {quoted}\n'
        f'nssm set Clauster AppDirectory "{wd}"\n'
        f"nssm set Clauster AppEnvironmentExtra CLAUSTER_CONFIG={cfg}\n"
        "nssm set Clauster Start SERVICE_AUTO_START\n"
        "nssm start Clauster\n"
    )
