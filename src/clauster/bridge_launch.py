"""Config-only launch/argv helpers for the standard and pty bridges (part of #1157).

:class:`BridgeLaunch` is the first collaborator extracted from
:class:`~clauster.runner.SessionRunner` (issue #1157). It builds the two bridge
argvs and launches the detached bridge / keeper subprocesses. It reads
configuration and paths ONLY — it owns no bridge registry, no locks, and no
lifecycle state, so ``SessionRunner`` keeps sole ownership of those. The runner
holds one instance as ``self._launch`` and delegates the moved methods to it,
preserving the exact public signatures the tests call directly.

The two bridge modes stay separate here exactly as they were on the runner and as
``AGENTS.md`` requires: :meth:`BridgeLaunch._build_cmd` / :meth:`BridgeLaunch._popen`
serve the standard (``claude remote-control`` subcommand) bridge, while
:meth:`BridgeLaunch._build_pty_bridge_argv` / :meth:`BridgeLaunch._keeper_launch_cmd`
/ :meth:`BridgeLaunch._popen_keeper` serve the pty keeper (``claude --remote-control``
flag form). They share no builder — do not unify them.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from . import procutil
from .claude_cli import resolve_binary
from .config import (
    INHERIT_PERMISSION_MODE,
    ClausterConfig,
    PermissionMode,
    SandboxMode,
    SpawnMode,
)


class BridgeLaunch:
    """Build bridge argv and launch detached bridge/keeper subprocesses (config-only)."""

    def __init__(
        self,
        *,
        config: ClausterConfig,
        binary: str,
        log_dir: Path,
        stderr_path_for: Callable[[Path], Path],
    ) -> None:
        """Bind to config, the configured binary, the log dir, and the stderr-path helper.

        ``stderr_path_for`` is :meth:`SessionRunner._stderr_path_for`, kept on the
        runner (its non-launch callers still use it) and injected here so :meth:`_popen`
        has a single source of truth for the captured-stderr sibling path.
        """
        self._config = config
        self._binary = binary
        self._log_dir = log_dir
        self._stderr_path_for = stderr_path_for
        # Monotonic spawn counter → unique log filenames even for two same-ms spawns.
        self._log_seq = 0

    def _unique_log_path(self, name: str) -> Path:
        """Return a log path unique to this spawn, so no two spawns can share a stem."""
        self._log_dir.mkdir(parents=True, exist_ok=True)
        # Unique per spawn so the parser never reads a previous run's markers — AND so the
        # 0600 O_EXCL pre-create can't FileExistsError. The millisecond timestamp alone
        # collides for two same-project spawns in the same ms (and a retry on it wouldn't
        # advance the clock), so a monotonic per-runner counter guarantees a fresh path
        # every call. _unique_log_path only runs on the event loop, so the bump is safe.
        self._log_seq += 1
        return self._log_dir / f"{name}-{int(time.time() * 1000)}-{self._log_seq}.log"

    def _build_cmd(
        self,
        log_path: Path,
        name: str,
        spawn_mode: SpawnMode,
        permission_mode: PermissionMode,
        sandbox: SandboxMode = "default",
    ) -> list[str]:
        """Build the `claude remote-control` argv. Pure (no side effects) so it's unit-testable.

        ``name`` becomes ``--name`` verbatim — the caller (:meth:`_spawn_locked`) has
        already resolved it to either the operator's custom bridge name or the
        project name (#780, via :func:`_normalize_custom_name`).

        ``sandbox`` (#780) adds the OS-level filesystem/network isolation flag:
        ``"on"`` → ``--sandbox``, ``"off"`` → ``--no-sandbox``, ``"default"`` → neither
        (claude's own off-by-default / ``sandbox.*`` settings apply — zero change).
        These are real, documented flags on ``claude remote-control`` (verified on
        claude 2.1.198; see the docs at code.claude.com/docs/en/remote-control).
        """
        defaults = self._config.instance_defaults
        cmd = [
            self._binary,
            "remote-control",
            "--name",
            name,
            "--debug-file",
            str(log_path),
            "--spawn",
            spawn_mode,
        ]
        # "inherit" (#1231) is Clauster's own sentinel, never a claude mode string: emit
        # NO --permission-mode at all so the session starts in its own default and carries
        # none of the flag's spawn-time system-prompt effect. Every other value is one of
        # the six real modes, already screened by _validate_spawn_options.
        if permission_mode != INHERIT_PERMISSION_MODE:
            cmd += ["--permission-mode", permission_mode]
        # Sandbox toggle (#780) — append only for an explicit on/off; "default" leaves it
        # to claude's own setting. Placed before the config-driven flags below, order is
        # immaterial to claude's parser. Disabled for 1.0 (#1037) not here but at the source:
        # `sandbox` is coerced to "default" on the way in (fresh spawn) and on persisted read,
        # so nothing but "default" reaches this builder until #1046 re-enables the toggle —
        # keeping this low-level emission intact and directly tested for that re-enable.
        if sandbox == "on":
            cmd += ["--sandbox"]
        elif sandbox == "off":
            cmd += ["--no-sandbox"]
        # Brand auto-generated session names when configured. Multi-session modes only
        # (same-dir/worktree) — `session` is single-session, so the prefix is out of scope.
        if defaults.session_name_prefix and spawn_mode in ("same-dir", "worktree"):
            cmd += ["--remote-control-session-name-prefix", defaults.session_name_prefix]
        # --capacity caps concurrent sessions inside a same-dir/worktree bridge; it does
        # not apply to the single-session `session` spawn mode, so don't pass it there.
        if spawn_mode in ("same-dir", "worktree"):
            cmd += ["--capacity", str(defaults.capacity)]
        # Permanent opt-in observability toggle: detailed connection/session logging
        # for the standard bridge (every spawn mode). Gated on config, never
        # unconditional; off by default. The pty bridge is never passed --verbose.
        if defaults.verbose:
            cmd += ["--verbose"]
        return cmd

    def _bridge_env_overlay(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Build the config-driven env overlay (``claude.path_append`` / ``claude.env``).

        Returns an ``extra`` mapping for :func:`procutil.child_env`, merging the
        operator's ``claude.env`` and a ``PATH`` extended by ``claude.path_append``
        (plus, when ``claude.node_from_nvm`` is on, nvm's resolved ``default`` node
        bin dir *prepended* so it wins over a distro node — see
        :func:`procutil.resolve_nvm_default_node_bin_dir` and issues #792 / #1018)
        with any caller ``extra`` (e.g. resume-recap flags). Passing
        it through ``child_env`` re-scrubs Clauster secrets, so config can never
        re-introduce a scrubbed credential name.
        """
        claude = self._config.claude
        path_append = list(claude.path_append)
        path_prepend: list[str] = []
        if claude.node_from_nvm:
            # Process-memoized (procutil.cached_…): the resolver shells bash (up to its
            # timeout on a slow $NVM_DIR), so the spawn path and the doctor panel share ONE
            # probe rather than re-shelling per spawn/request (#792, Greptile #803/#859).
            nvm_bin_dir = procutil.cached_nvm_default_node_bin_dir()
            if nvm_bin_dir:
                # PREPEND, not append (#1018): appending landed nvm's dir AFTER the
                # inherited service PATH, so a distro `/usr/bin/node` earlier on PATH
                # shadowed nvm's `default` node — reintroducing the exact resolution
                # failure node_from_nvm exists to fix, now with a WRONG node. The whole
                # point of the knob is to use nvm's node, so it must win.
                path_prepend.append(nvm_bin_dir)
        return procutil.bridge_env_overlay(
            path_append=path_append, path_prepend=path_prepend, env=claude.env, extra=extra
        )

    def _popen(
        self,
        cwd: Path,
        log_path: Path,
        name: str,
        spawn_mode: SpawnMode,
        permission_mode: PermissionMode,
        debug_path: Path | None = None,
        sandbox: SandboxMode = "default",
    ) -> subprocess.Popen:
        """Launch the detached bridge subprocess with a resolved binary and scrubbed env."""
        # The bridge writes its --debug-file to `debug_path` (the private raw parse-
        # source when on-disk redaction is on); the captured-stderr sibling stays keyed
        # off the public `log_path`. They coincide when redaction is off.
        cmd = self._build_cmd(debug_path or log_path, name, spawn_mode, permission_mode, sandbox)
        # Exec the RESOLVED absolute path, not the bare configured name: Windows
        # CreateProcess only auto-appends .exe (never the .cmd/.ps1 shim npm installs
        # for `claude`), so a bare name that the version probe resolves via
        # shutil.which would fail to spawn here. Also pins the binary we validated.
        cmd[0] = resolve_binary(cmd[0])
        # Always build the child env from the SCRUBBED base (procutil.child_env)
        # so the bridge — which runs project-controlled code — can never read a
        # Clauster secret (session signing key, password hash) from its own
        # os.environ. When resume-recap is enabled, flag it in the bridge's env:
        # the detached bridge's child sessions inherit this, and the SessionStart
        # hook (wired into ~/.claude/settings.json) acts only when it is set — so
        # the recap never fires for the user's non-Clauster sessions sharing that
        # config. The recap flags overlay AFTER scrubbing, so they are never lost.
        recap_env: dict[str, str] = {}
        if self._config.claude.resume_recap:
            recap_env = {
                "CLAUSTER_RESUME_RECAP": "1",
                "CLAUSTER_RESUME_RECAP_MAX_CHARS": str(self._config.claude.resume_recap_max_chars),
            }
        # Overlay the operator's PATH/env extensions (claude.path_append/claude.env)
        # on top of the recap flags; child_env re-scrubs secrets so config can never
        # re-introduce a scrubbed credential name.
        popen_env = procutil.child_env(self._bridge_env_overlay(recap_env))
        # Capture stdout+stderr to a file so a failed start leaves a diagnosable
        # reason behind (the bridge logs the *why* there, not to --debug-file).
        # The detached child inherits its own dup of the fd, so the parent closes
        # its copy right after spawn; the child keeps writing.
        err_fh = self._stderr_path_for(log_path).open("wb")
        try:
            # Detach the bridge into its own session/group so it survives a clauster
            # restart and a SIGINT to clauster never propagates to it. On Windows,
            # CREATE_NEW_PROCESS_GROUP additionally makes the bridge addressable by a
            # CTRL_BREAK_EVENT for graceful stop (POSIX uses start_new_session); stdin
            # is detached so a wrapping cmd.exe never blocks on an interactive prompt.
            if sys.platform == "win32":
                return subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=err_fh,
                    stderr=subprocess.STDOUT,
                    env=popen_env,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            return subprocess.Popen(  # pragma: skip-on-win
                cmd,
                cwd=str(cwd),
                stdout=err_fh,
                stderr=subprocess.STDOUT,
                env=popen_env,
                start_new_session=True,
            )
        finally:
            err_fh.close()

    def _build_pty_bridge_argv(
        self,
        log_path: Path,
        name: str,
        permission_mode: PermissionMode,
        *,
        resume: bool,
        resume_session_id: str | None = None,
        worktree_name: str | None = None,
    ) -> list[str]:
        """Build the flag-form bridge argv (`claude --remote-control …`). Pure/testable.

        Unlike the subcommand (`_build_cmd`), the flag form is a single interactive
        session — no `--spawn`/`--capacity`. ``--continue`` (on resume) is what makes
        the restarted session restore its prior conversation context.
        ``resume_session_id`` (#303, fresh spawns only — the ``resume`` revive path
        takes precedence and never carries one, enforced upstream) forks an
        operator-picked PAST conversation into this NEW session:
        ``--resume <uuid> --fork-session`` — fork mints a fresh session id, so the
        picked conversation itself is never clobbered (probed on claude 2.1.211).
        ``worktree_name`` (spawn_mode="worktree", #779) adds ``--worktree <name>`` so
        claude runs the session in its own git worktree under
        ``<repo>/.claude/worktrees/<name>`` — a repeated name REUSES that worktree
        (empirically verified), so the same instance's resume (``--continue`` +
        the same name, stable via its instance_id) restores the conversation IN it.
        """
        argv = [
            self._binary,
            "--remote-control",
            name,
            "--debug-file",
            str(log_path),
        ]
        # Same "inherit" sentinel handling as the subcommand form (#1231) — kept here
        # rather than factored out, because the two bridge argvs stay deliberately
        # separate (they share no builder).
        if permission_mode != INHERIT_PERMISSION_MODE:
            argv += ["--permission-mode", permission_mode]
        if worktree_name is not None:
            argv += ["--worktree", worktree_name]
        if resume:
            argv.append("--continue")
        elif resume_session_id is not None:
            argv += ["--resume", resume_session_id, "--fork-session"]
        return argv

    @staticmethod
    def _keeper_launch_cmd(
        sidecar: Path,
        cwd: Path,
        bridge_argv: list[str],
        screen_sidecar: Path | None = None,
        *,
        state_dir: Path,
    ) -> list[str]:
        """Wrap the bridge argv in a PTY-keeper launcher.

        Source/venv: ``<python> -m clauster.pty_keeper …``. A frozen (PyInstaller)
        binary can't use ``-m`` — ``sys.executable`` is the clauster binary, whose
        argparse rejects it — so it re-invokes itself with the hidden
        :data:`~clauster.procutil.KEEPER_SUBCOMMAND` (routed in
        :func:`clauster.__main__.main`, mirroring the recap hook).

        ``--state-dir`` is passed so the keeper can add the managed ``<state_dir>/deps``
        directory to its ``sys.path`` before it builds the pyte screen (#1486). The keeper
        is dispatched before the server's own ``add_deps_dir_to_sys_path``, so without this
        a ``clauster deps install pty``'d pyte never loads in the frozen binary's keeper.
        """
        if getattr(sys, "frozen", False):
            launcher = [sys.executable, procutil.KEEPER_SUBCOMMAND]
        else:
            launcher = [sys.executable, "-m", "clauster.pty_keeper"]
        cmd = [
            *launcher,
            "--sidecar",
            str(sidecar),
            "--cwd",
            str(cwd),
            "--state-dir",
            str(state_dir),
        ]
        if screen_sidecar is not None:
            cmd += ["--screen-sidecar", str(screen_sidecar)]
        cmd += ["--", *bridge_argv]
        return cmd

    def _popen_keeper(
        self,
        cwd: Path,
        sidecar: Path,
        bridge_argv: list[str],
        screen_sidecar: Path | None = None,
        *,
        state_dir: Path,
    ) -> subprocess.Popen:
        """Launch the PTY keeper detached so it outlives a Clauster restart.

        Same detached pattern as the subcommand `_popen` (own session/process group,
        stdout+stderr to a file), plus stdin detached on EVERY platform — `_popen` does
        that only on Windows. The keeper, not Clauster, holds the bridge's terminal, so
        it survives independently and keeps the bridge alive.

        ``state_dir`` is threaded to the keeper so a side-installed ``pyte`` loads (#1486).
        """
        cmd = self._keeper_launch_cmd(
            sidecar, cwd, bridge_argv, screen_sidecar, state_dir=state_dir
        )
        keeper_log = sidecar.with_suffix(".log")  # the keeper's own stdout/stderr
        err_fh = keeper_log.open("wb")
        try:
            # Overlay the operator's PATH/env extensions onto the KEEPER's env: the
            # keeper inherits them into its own os.environ and re-emits them (still
            # secret-scrubbed) when it spawns the bridge via child_env(), so the pty
            # bridge gets the same extended PATH/env as the standard path.
            keeper_env = procutil.child_env(self._bridge_env_overlay())
            # Detach the keeper so it outlives a Clauster restart. POSIX: its own session
            # (setsid). Windows: DETACHED_PROCESS drops the shared console so a clauster
            # exit / CTRL can't reach it, plus CREATE_NEW_PROCESS_GROUP for a clean group
            # (start_new_session is a POSIX no-op there).
            if sys.platform == "win32":
                return subprocess.Popen(
                    cmd,
                    cwd=str(cwd),
                    stdin=subprocess.DEVNULL,
                    stdout=err_fh,
                    stderr=subprocess.STDOUT,
                    env=keeper_env,
                    creationflags=(
                        subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                    ),
                )
            return subprocess.Popen(  # pragma: skip-on-win
                cmd,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=err_fh,
                stderr=subprocess.STDOUT,
                env=keeper_env,
                start_new_session=True,
            )
        finally:
            err_fh.close()
