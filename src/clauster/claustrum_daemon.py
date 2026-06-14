"""Connect-or-spawn lifecycle for the per-deployment claustrum daemon (CL-2).

CL-1 (:mod:`clauster.claustrum_client`) provides a client for an *already
running* daemon. This module owns the daemon's lifecycle: one ``claustrum``
process per Clauster deployment, started lazily at app startup when
``claustrum.enabled`` is set, with its socket + auth token living under
``<state_dir>/claustrum/``.

The handshake is **connect-or-spawn**, in that order:

* If a daemon is already listening (e.g. it outlived a Clauster restart — it
  self-daemonizes with ``setsid``), Clauster simply reconnects with the token it
  persisted on disk. Nothing is spawned.
* Otherwise Clauster spawns ``claustrum -serve -socket <sock> -token-fd 0``,
  hands the token over the child's stdin pipe (never argv/env/logs), waits for
  the launcher to detach (it exits 0 once the real daemon is reparented to
  ``init``), then polls the socket until the first authed ping succeeds.

Fail-closed: a daemon that cannot be reached or rejects the token leaves
:meth:`ClaustrumDaemon.status` carrying the error and never raises into the app
lifespan — bridges are unaffected and (future) hosted spawns are refused. The
daemon is intentionally **left running** on :meth:`ClaustrumDaemon.aclose`, the
same way Clauster leaves bridges running across its own restarts.

POSIX-only: claustrum speaks ``AF_UNIX`` and self-daemonizes via ``setsid``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
import stat
from pathlib import Path
from typing import Any

from . import procutil
from .claustrum_client import (
    AuthRejected,
    ClaustrumClient,
    ClaustrumError,
    DaemonUnreachable,
)
from .config import ClausterConfig

logger = logging.getLogger(__name__)

# How long to nap between socket probes while waiting for a freshly spawned
# daemon to come up. Short enough to feel instant; the overall wait is bounded by
# ``claustrum.spawn_timeout_seconds``.
_POLL_INTERVAL = 0.1

# Bounded timeout for a liveness ping (revalidating a cached connection / serving
# ``/healthz``). Kept short so a hung daemon can't stall a health check; a dead
# socket fails the ping near-instantly regardless.
_HEALTH_PING_TIMEOUT = 2.0

# Env vars that must NOT leak into a spawned ``claustrum``. ``CLAUDE_SSH_DAEMON_CHILD``
# is claustrum's internal "I am the re-exec'd daemon child" sentinel — the SAME name
# the ambient claude-ssh / ccd-cli daemon exports to its descendants. If Clauster is
# started from inside such a session (interactive / agent terminals; NOT systemd,
# whose env is clean), the launcher would inherit it ``=1``, skip its ``-token-fd``
# read, and exit 1 (``read --token-file: open : no such file``). Stripping the
# sentinel(s) makes the launcher always run its parent branch. The token travels on
# stdin, never env, so this is auth-neutral. ``CLAUSTRUM_DAEMON_CHILD`` is included so
# Clauster stays correct after claustrum namespaces its own sentinel;
# ``CLAUSTRUM_TOKEN_PIPE`` is hygiene (a leaked pipe-fd marker would mislead the child).
# See ``scratch/claustrum-token-fd-env-collision.md``.
_DAEMON_SENTINEL_ENV = frozenset(
    {"CLAUDE_SSH_DAEMON_CHILD", "CLAUSTRUM_DAEMON_CHILD", "CLAUSTRUM_TOKEN_PIPE"}
)


class DaemonSpawnError(ClaustrumError):
    """The ``claustrum -serve`` launcher failed to start a usable daemon."""


class ClaustrumDaemon:
    """Connect-or-spawn manager for one deployment's claustrum daemon.

    Construct from the parsed config, then :meth:`ensure` (idempotent) at app
    startup; read :meth:`status` for health and :meth:`client` for the live
    connection. :meth:`aclose` drops Clauster's connection but leaves the daemon
    running so it survives a restart.
    """

    def __init__(self, config: ClausterConfig) -> None:
        """Derive socket/token paths from the config; nothing is started yet."""
        self._config = config
        self._cfg = config.claustrum
        self._dir = Path(config.state_dir).expanduser() / "claustrum"
        self._socket = (
            Path(self._cfg.socket_path) if self._cfg.socket_path else self._dir / "daemon.sock"
        )
        self._token_path = self._dir / "token"
        self._log_path = self._dir / "daemon.log"
        self._token: str | None = None
        self._client: ClaustrumClient | None = None
        self._version: str | None = None
        self._error: str | None = None
        # Serializes every lifecycle transition (ensure / probe / aclose) so
        # concurrent callers can't race into double-spawns or close a connection
        # another waiter just established.
        self._lock = asyncio.Lock()

    @property
    def client(self) -> ClaustrumClient | None:
        """The live daemon connection, or ``None`` if not connected."""
        return self._client

    @property
    def socket_path(self) -> Path:
        """The AF_UNIX socket path this daemon listens on."""
        return self._socket

    def status(self) -> dict[str, Any]:
        """Return a small, log-safe health dict (never includes the token)."""
        return {
            "enabled": True,
            "running": self._client is not None,
            "socket": str(self._socket),
            "version": self._version,
            "error": self._error,
        }

    async def ensure(self) -> ClaustrumClient:
        """Connect to a running daemon, or spawn one and connect (idempotent).

        A cached connection is revalidated with a bounded ping before reuse — a
        daemon that died after startup is dropped and reconnected/respawned
        rather than handed back dead. Serialized by the lifecycle lock so
        concurrent callers spawn at most one daemon.

        Returns the live :class:`ClaustrumClient`. Raises :class:`AuthRejected`
        if a running daemon rejects the persisted token (Clauster must not spawn
        a second daemon over a healthy one), :class:`DaemonSpawnError` if the
        launcher fails, or :class:`DaemonUnreachable` if a freshly spawned daemon
        never accepts a connection. On any failure :meth:`status` carries the
        reason.
        """
        async with self._lock:
            if self._client is not None:
                if await self._is_alive(self._client):
                    return self._client
                # Cached connection is dead — drop it and reconnect/spawn below.
                await self._client.close()
                self._client = None
            return await self._connect_or_spawn()

    async def probe(self) -> dict[str, Any]:
        """Live health for ``/healthz``: ping the cached client, drop it if dead.

        Unlike :meth:`ensure` this never reconnects or spawns — it reports the
        current truth and clears a dead connection so the next :meth:`ensure`
        recovers. (Background auto-reconnect without a caller is CL-6.)
        """
        async with self._lock:
            if self._client is not None and not await self._is_alive(self._client):
                await self._client.close()
                self._client = None
                self._error = "claustrum daemon connection lost"
            return self.status()

    async def aclose(self) -> None:
        """Drop Clauster's connection; leave the daemon running."""
        async with self._lock:
            if self._client is not None:
                await self._client.close()
                self._client = None

    async def _connect_or_spawn(self) -> ClaustrumClient:
        """Connect to a running daemon or spawn one (caller holds the lock)."""
        self._error = None
        self._prepare_dir()
        self._token = token = self._read_or_create_token()

        try:
            self._client = await self._connect(token)
            logger.info("claustrum: connected to existing daemon at %s", self._socket)
            return self._client
        except AuthRejected as exc:
            # A daemon is up but holds a different token (e.g. the token file was
            # regenerated out from under a still-running daemon). Spawning over it
            # would orphan the live one — surface instead.
            self._error = "running claustrum daemon rejected the persisted token"
            raise AuthRejected(str(exc)) from exc
        except DaemonUnreachable:
            pass  # not running — spawn it below

        # One shared budget across launcher-detach + first-connect, so the total
        # startup wait is bounded by spawn_timeout_seconds (not 2× it).
        deadline = asyncio.get_running_loop().time() + self._cfg.spawn_timeout_seconds
        await self._spawn(token, deadline)
        self._client = await self._poll_connect(token, deadline)
        logger.info("claustrum: spawned daemon at %s", self._socket)
        return self._client

    async def _is_alive(self, client: ClaustrumClient) -> bool:
        """Whether a connection still answers a bounded liveness ping."""
        try:
            await asyncio.wait_for(client.ping(), timeout=_HEALTH_PING_TIMEOUT)
        except (ClaustrumError, TimeoutError):
            return False
        return True

    # -- internals ---------------------------------------------------------

    def _prepare_dir(self) -> None:
        """Create ``<state_dir>/claustrum/`` 0700 (tighten an existing one too)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        try:
            self._dir.chmod(0o700)
        except OSError:  # pragma: no cover - chmod can fail on exotic filesystems
            logger.warning("claustrum: could not chmod %s to 0700", self._dir)

    def _read_or_create_token(self) -> str:
        """Read the persisted auth token, generating + persisting one if absent.

        The token is what lets Clauster reconnect to a daemon that outlived it,
        so it is stored (0600) rather than regenerated each run.
        """
        if self._token_path.exists():
            token = self._token_path.read_text(encoding="utf-8").strip()
            if token:
                return token
        token = secrets.token_hex(32)
        # Create with 0600 from the start so the token is never briefly world-readable.
        fd = os.open(self._token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
        self._token_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return token

    def _resolve_binary(self) -> str:
        resolved = shutil.which(self._cfg.binary)
        if resolved is None:
            raise DaemonSpawnError(f"claustrum binary not found: {self._cfg.binary!r}")
        return resolved

    async def _connect(self, token: str) -> ClaustrumClient:
        """Dial the socket and verify with an authed ping + version probe."""
        client = ClaustrumClient(
            str(self._socket),
            token,
            request_timeout=self._cfg.request_timeout_seconds,
        )
        await client.connect()
        try:
            await client.ping()
            version = await client.version()
        except BaseException:
            await client.close()
            raise
        self._version = version.get("version") if isinstance(version, dict) else None
        return client

    async def _spawn(self, token: str, deadline: float) -> None:
        """Launch ``claustrum -serve``, feeding the token over the stdin pipe.

        The launcher reads the token from fd 0, re-execs a detached child
        (reparented to ``init``), and exits 0; the child opens the socket. We
        wait only for the launcher to exit (until ``deadline``), then poll the
        socket separately — the daemon's own stdout/stderr go to ``daemon.log``
        so the detached, long-lived child never blocks on a pipe we'd drain.
        """
        binary = self._resolve_binary()
        # -keep-children (CL-8): a daemon restart/upgrade then leaves hosted child
        # sessions running for Clauster to reattach/recover. POSIX-only — the daemon
        # ignores it with a warning on Windows.
        argv = [binary, "-serve", "-socket", str(self._socket), "-token-fd", "0"]
        if self._cfg.keep_children:
            argv.append("-keep-children")
        # Defensive: scrub claustrum's daemonize sentinel from the child env (see
        # _DAEMON_SENTINEL_ENV) so an ambient CLAUDE_SSH_DAEMON_CHILD can't make the
        # launcher mistake itself for its own re-exec'd child and skip the token read.
        env = procutil.child_env()  # scrub Clauster secrets; the daemon spawns hosted agents
        for sentinel in _DAEMON_SENTINEL_ENV:
            env.pop(sentinel, None)
        # Append-mode log: the detached daemon keeps writing here after we return.
        log_file = open(self._log_path, "ab")  # noqa: SIM115 - handed to the child; closed below
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
                env=env,
            )
        except OSError as exc:  # pragma: no cover - exec failure after which() resolved
            log_file.close()
            self._error = f"could not launch claustrum: {exc}"
            raise DaemonSpawnError(self._error) from exc

        stdin = proc.stdin
        if stdin is None:  # pragma: no cover - stdin=PIPE always yields a writer
            log_file.close()
            raise DaemonSpawnError("claustrum launcher exposes no stdin pipe")
        try:
            stdin.write(token.encode("utf-8") + b"\n")
            await stdin.drain()
            stdin.close()
        except (OSError, ConnectionError):  # pragma: no cover - racy; backstopped by returncode
            # The launcher may have already read its token and closed fd 0; any
            # real failure surfaces via the returncode / poll below.
            logger.debug("claustrum: writing token to launcher stdin failed (already closed?)")

        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        try:
            returncode = await asyncio.wait_for(proc.wait(), timeout=remaining)
        except TimeoutError as exc:
            proc.kill()
            self._error = "claustrum -serve did not detach within the spawn timeout"
            raise DaemonSpawnError(self._error) from exc
        finally:
            log_file.close()

        if returncode != 0:
            self._error = f"claustrum -serve exited {returncode}; see {self._log_path}"
            raise DaemonSpawnError(self._error)

    async def _poll_connect(self, token: str, deadline: float) -> ClaustrumClient:
        """Poll the socket until an authed connection succeeds or ``deadline``."""
        loop = asyncio.get_running_loop()
        last_exc: ClaustrumError | None = None
        while True:
            try:
                return await self._connect(token)
            except DaemonUnreachable as exc:
                last_exc = exc
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(_POLL_INTERVAL)
            except AuthRejected as exc:
                self._error = "spawned claustrum daemon rejected our token"
                raise AuthRejected(str(exc)) from exc
        self._error = "spawned claustrum daemon never accepted a connection"
        raise DaemonUnreachable(self._error) from last_exc
