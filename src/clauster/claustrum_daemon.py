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
* Otherwise Clauster spawns ``claustrum -serve -socket <sock> -token-fd 0``
  (plus ``-listen-pipe`` on Windows), hands the token over the child's stdin pipe
  (never argv/env/logs), waits for the launcher to detach (it exits 0 once the real
  daemon is detached — reparented to ``init`` on POSIX, ``DETACHED_PROCESS`` on
  Windows), then polls the transport until the first authed ping succeeds.

The daemon closes a connection after 5 minutes with no traffic (claustrum PR 289).
The client's keepalive ping prevents that, but a connection can still end (a daemon
restart, a crash). Each connection therefore has a watcher task: when the daemon ends
it, the watcher drops the dead client and reconnects through :meth:`ensure`. A
reconnect that replaced an earlier connection calls the ``on_reconnect`` hook, which
reattaches the running hosted sessions. A reconnect that fails calls
``on_unreachable``, which marks them as errors (fail closed).

Fail-closed: a daemon that cannot be reached or rejects the token leaves
:meth:`ClaustrumDaemon.status` carrying the error and never raises into the app
lifespan — bridges are unaffected and (future) hosted spawns are refused. The
daemon is intentionally **left running** on :meth:`ClaustrumDaemon.aclose`, the
same way Clauster leaves bridges running across its own restarts.

Cross-platform: claustrum speaks ``AF_UNIX`` on POSIX and a named pipe on Windows
(the client discovers it via ``rpc.pipe``, #893); the Windows spawn adds
``-listen-pipe``. It self-daemonizes on both — ``setsid`` on POSIX,
``DETACHED_PROCESS`` on Windows — so Clauster only waits for the launcher to exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import stat
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine
from pathlib import Path
from typing import Any

from . import deps, procutil
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

# How long a loser of the token-file O_EXCL race waits for the winner to finish
# writing its bytes before concluding the file is genuinely stale/blank
# (100 * 10ms = 1s). Mirrors auth._read_existing_secret: a momentarily-empty file
# is "winner mid-write", not "stale leftover" — never replace it until the wait
# is exhausted, or two starts diverge onto different tokens.
_TOKEN_READ_ATTEMPTS = 100
_TOKEN_READ_DELAY = 0.01

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
    """The claustrum daemon could not be started.

    Raised both when the binary cannot be resolved at all and when the ``-serve``
    launcher fails to hand off a usable daemon.
    """


# AF_UNIX ``sun_path`` is a fixed C buffer — 104 bytes on macOS/BSD, 108 on Linux; a longer
# socket path fails bind/connect with an opaque ``OSError``. Preflight against the SMALLER cap
# so a ``state_dir`` that works on Linux doesn't silently break on macOS (#914). Windows dials a
# named pipe (no such limit), so this is POSIX-only.
_SUN_PATH_MAX = 104


def _af_unix_in_use() -> bool:
    """Return whether claustrum dials an AF_UNIX socket (POSIX) vs a Windows named pipe.

    A seam so the ``sun_path`` length gate is testable on every OS *without* monkeypatching
    the global ``os.name`` — flipping that on Windows makes ``pathlib.Path`` pick an
    uninstantiable flavour and crashes the whole xdist worker.
    """
    return os.name == "posix"


def _check_unix_socket_path(sock: Path) -> None:
    """Raise a clear :class:`ClaustrumError` if the AF_UNIX socket path exceeds ``sun_path``."""
    if not _af_unix_in_use():
        return
    # ``os.fsencode`` counts the exact bytes the AF_UNIX layer binds (the filesystem
    # encoding), not whatever ``str.encode`` defaults to on a non-UTF-8 POSIX host.
    length = len(os.fsencode(sock))
    if length >= _SUN_PATH_MAX:
        raise ClaustrumError(
            f"claustrum socket path is {length} bytes, over the AF_UNIX limit of {_SUN_PATH_MAX} "
            f"— shorten `state_dir` (or set `claustrum.socket_path`). Path: {sock}"
        )


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
        _check_unix_socket_path(self._socket)  # fail early on a too-long AF_UNIX path (#914)
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
        # TASK-518: the watcher of the current connection, the background tasks it and
        # probe() start (reconnects and hook runs), and the hooks themselves.
        self._watch_task: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._hook_task: asyncio.Task[None] | None = None
        self._had_connection = False
        self._first_connect_failed = False
        self._closing = False
        self._on_reconnect: Callable[[ClaustrumClient], Awaitable[None]] | None = None
        self._on_unreachable: Callable[[str], Awaitable[None]] | None = None

    @property
    def client(self) -> ClaustrumClient | None:
        """The live daemon connection, or ``None`` if not connected."""
        return self._client

    @property
    def socket_path(self) -> Path:
        """The daemon's socket path — the AF_UNIX endpoint on POSIX.

        On Windows the client instead dials the named pipe advertised in the ``rpc.pipe``
        file beside this path.
        """
        return self._socket

    def set_connection_hooks(
        self,
        *,
        on_reconnect: Callable[[ClaustrumClient], Awaitable[None]],
        on_unreachable: Callable[[str], Awaitable[None]],
    ) -> None:
        """Register what runs when the daemon connection is replaced or cannot come back.

        ``on_reconnect`` gets the new client for each new connection, except the first
        one when the first :meth:`ensure` attempt made it: the app lifespan reattaches
        the stored sessions over that one itself. A first connection made after that
        attempt failed does run the hook, because no startup reattach happened.
        ``on_unreachable`` gets the reason when a reconnect after an earlier connection
        fails. Both run in a task this manager owns, one run at a time and in order, and
        :meth:`aclose` cancels a run still in progress.
        """
        self._on_reconnect = on_reconnect
        self._on_unreachable = on_unreachable

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
        concurrent callers spawn at most one daemon. Each new connection gets a
        watcher (:meth:`_watch`). A connection that replaced an earlier one schedules
        the ``on_reconnect`` hook; a failed reconnect after an earlier connection
        schedules ``on_unreachable``. Neither runs in the caller's task (see
        :meth:`_schedule_hook`), so a hosted spawn never waits for, or cancels, the
        reattach of other sessions.

        Returns the live :class:`ClaustrumClient`. Raises :class:`AuthRejected`
        if a running daemon rejects the persisted token (Clauster must not spawn
        a second daemon over a healthy one), :class:`DaemonSpawnError` if the
        launcher fails, or :class:`DaemonUnreachable` if a freshly spawned daemon
        never accepts a connection. **Every** failure path records the reason for
        :meth:`status`, so ``/healthz`` can name what went wrong rather than reporting
        ``running:false, error:null`` and leaving the cause in the lifespan log.
        """
        async with self._lock:
            if self._closing:
                # A late caller after aclose() would start a watcher nobody cancels.
                raise DaemonUnreachable("claustrum daemon manager is shut down")
            try:
                if self._client is not None:
                    if await self._is_alive(self._client):
                        return self._client
                    # Cached connection is dead — drop it and reconnect/spawn below.
                    await self._drop_client(self._client)
                client = await self._connect_or_spawn()
            except Exception as exc:
                if self._had_connection:
                    # Sessions may still sit on the connection that ended, and it cannot
                    # be replaced: fail them visibly rather than leave them "running".
                    self._schedule_hook(
                        self._on_unreachable,
                        f"claustrum daemon connection lost and the reconnect failed: {exc}",
                    )
                else:
                    self._first_connect_failed = True
                raise
            except BaseException:
                # Cancelled (a hosted spawn whose client went away) after the dead client
                # was dropped: finish the reconnect in the background, or the sessions on
                # it would wait for the next spawn to call ensure().
                if self._had_connection and self._client is None and not self._closing:
                    self._track(self._recover())
                raise
            # The first connection the app's startup made needs no hook: the lifespan
            # reattaches the stored sessions over it. Any other new connection does,
            # including a first one that came after the startup attempt failed.
            replaced = self._had_connection or self._first_connect_failed
            self._had_connection = True
            self._start_watch(client)
            if replaced:
                self._schedule_hook(self._on_reconnect, client)
        return client

    async def probe(self) -> dict[str, Any]:
        """Live health for ``/healthz``: ping the cached client, drop it if dead.

        It reports the current truth and never makes the caller wait for a reconnect.
        A connection the daemon *closed* never gets here, because its watcher
        reconnects on the EOF. This catches a connection that is open but no longer
        answers: it is dropped, and a background :meth:`ensure` reconnects it and runs
        the hooks, the same recovery the watcher runs. Without that, the sessions on
        the dropped connection stayed "running" until some spawn happened to call
        :meth:`ensure`.
        """
        async with self._lock:
            if self._client is not None and not await self._is_alive(self._client):
                # Recovery first, before any await of the drop: a probe cancelled inside
                # the close would otherwise leave no one to reconnect. The task waits on
                # this lock. No guard needed: ensure() is serialized, so a recovery that
                # overlaps one still running finds the client already back, and one that
                # starts during aclose() finds ensure() refusing.
                self._track(self._recover())
                self._error = "claustrum daemon connection lost"
                await self._drop_client(self._client)
            return self.status()

    async def aclose(self) -> None:
        """Drop Clauster's connection and stop its background tasks; leave the daemon up."""
        self._closing = True
        tasks = [t for t in (self._watch_task, *self._tasks) if t is not None]
        self._watch_task = None
        current = asyncio.current_task()
        tasks = [t for t in tasks if t is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            if self._client is not None:
                await self._drop_client(self._client)

    async def _drop_client(self, client: ClaustrumClient) -> None:
        """Close the cached ``client`` and stop its watcher (caller holds the lock).

        The watcher is cancelled unless it is the caller: a watcher dropping its own
        dead client goes on to reconnect, and ends once it has.
        """
        self._client = None
        watcher = self._watch_task
        if watcher is not None and watcher is not asyncio.current_task():
            watcher.cancel()
            self._watch_task = None
        await client.close()

    def _start_watch(self, client: ClaustrumClient) -> None:
        """Start the watcher for a new connection (caller holds the lock).

        Every path that replaces a client runs :meth:`_drop_client` first, so the
        previous watcher is already cancelled, or it is the caller and ends by itself.
        """
        self._watch_task = asyncio.create_task(self._watch(client))

    async def _watch(self, client: ClaustrumClient) -> None:
        """Wait for the daemon to end ``client``'s connection, then reconnect.

        The daemon sends no frame when it closes a connection, so the reader's EOF
        is the only signal. Without this task a dead client stayed cached: every
        later hosted spawn wrote into it and failed, and every running session lost
        its stream with its row still showing "running" (TASK-518).
        """
        await client.wait_lost()
        async with self._lock:
            if self._client is not client or self._closing:
                return  # already replaced or shut down by someone else
            await self._drop_client(client)
            self._error = "claustrum daemon connection lost"
        logger.warning("claustrum: the daemon closed the connection; reconnecting")
        await self._recover()

    async def _recover(self) -> None:
        """Reconnect after a dropped connection; :meth:`ensure` schedules the hook.

        Any failure is logged, never raised: this runs in a background task, where
        an exception would end it unread. ``ensure`` has already scheduled
        ``on_unreachable`` for it.
        """
        try:
            await self.ensure()
        except Exception as exc:
            logger.warning("claustrum: reconnect after a lost connection failed: %s", exc)

    def _track(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        """Run ``coro`` as a background task that :meth:`aclose` cancels."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _schedule_hook(self, hook: Callable[[Any], Awaitable[None]] | None, arg: Any) -> None:
        """Run a connection hook in a background task, after the run before it.

        Not in the caller's task: the caller is often a hosted spawn request, which
        would otherwise wait for every session's reattach, and whose cancellation (a
        client disconnect) would cut a reattach short. In order: two quick reconnects
        must not let the older run move sessions onto the older, dead client last.
        """
        if hook is None:
            return
        self._hook_task = self._track(self._run_hook(self._hook_task, hook, arg))

    @staticmethod
    async def _run_hook(
        previous: asyncio.Task[None] | None,
        hook: Callable[[Any], Awaitable[None]],
        arg: Any,
    ) -> None:
        """Wait for the ``previous`` hook run, then run ``hook``; log a fault in it."""
        if previous is not None:
            await asyncio.wait([previous])
        try:
            await hook(arg)
        except Exception:
            logger.exception("claustrum: connection hook failed")

    async def _connect_or_spawn(self) -> ClaustrumClient:
        """Connect to a running daemon or spawn one (caller holds the lock)."""
        self._error = None
        self._prepare_dir()
        # Off-loop: _read_or_create_token can block up to ~1s on the O_EXCL-loser path
        # (it polls for the winner's write). Running it inline would stall every live
        # WebSocket / HTTP handler on the single event loop for that window.
        self._token = token = await asyncio.to_thread(self._read_or_create_token)

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
        except OSError:
            # Best-effort tightening: a chmod failure (odd fs / perms) is logged, not fatal.
            logger.warning("claustrum: could not chmod %s to 0700", self._dir)

    def _read_or_create_token(self) -> str:
        """Read the persisted auth token, generating + persisting one if absent.

        The token is what lets Clauster reconnect to a daemon that outlived it,
        so it is stored (0600) rather than regenerated each run.

        Creation uses ``O_CREAT|O_EXCL`` (mirroring
        :func:`auth.load_or_create_secret`): an ``exists()``-then-``O_TRUNC`` race
        between two starts (or a restart that overlaps a live daemon) could
        otherwise truncate the file mid-write and clobber the token the running
        daemon still authenticates with. Only the ``O_EXCL`` winner writes; every
        loser **waits out** the winner's write (a momentarily-blank file is the
        winner mid-write, not stale) and adopts the winner's token, so all callers
        converge on one value. A file that stays blank for the whole wait is a truly
        abandoned/crashed create — only then is it atomically replaced.
        """
        existing = self._read_existing_token()
        if existing is not None:
            return existing
        token = secrets.token_hex(32)
        # Create with 0600 from the start so the token is never briefly world-readable.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(self._token_path, flags, 0o600)
        except FileExistsError:
            # The file already exists: a racing start (or a daemon that outlived us)
            # won the O_EXCL create. It may be MID-WRITE (created empty, bytes not yet
            # flushed) — so wait out the winner before deciding the file is stale.
            won = self._wait_for_winner_token()
            if won is not None:
                return won  # adopt the winner's token — never clobber a live credential
            # Genuinely blank after the full wait: an abandoned/crashed create. Only
            # now is an atomic replace safe.
            return self._replace_blank_token(token)
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
        self._token_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return token

    def _wait_for_winner_token(self) -> str | None:
        """Poll for an O_EXCL winner's token, returning it once written (else None).

        The winner creates the file empty then writes its bytes — a loser that raced
        in can momentarily read a blank file. Retry over a bounded window (mirroring
        ``auth._read_existing_secret``) so a winner mid-write is waited out rather than
        mistaken for stale leftover. Returns None only if the file stays blank for the
        whole window (a truly abandoned create).
        """
        for _ in range(_TOKEN_READ_ATTEMPTS):
            token = self._read_existing_token()
            if token is not None:
                return token
            time.sleep(_TOKEN_READ_DELAY)
        return None

    def _replace_blank_token(self, token: str) -> str:
        """Atomically replace a confirmed-stale/blank token file with ``token`` (0600).

        Only call this AFTER waiting out a possible mid-write winner
        (:meth:`_wait_for_winner_token`). Write to a sibling temp file then
        ``os.replace`` it into place: the rename is atomic, so a concurrent reader
        sees either the old or the new file whole, never a half-written one. A
        last-moment winner check still adopts a token that landed during the wait.
        """
        won = self._read_existing_token()
        if won is not None:  # a winner filled it at the last moment — adopt it
            return won
        tmp = self._token_path.with_name(f"{self._token_path.name}.{secrets.token_hex(4)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(tmp, flags, 0o600)
        try:
            try:
                os.write(fd, token.encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(tmp, self._token_path)
        except BaseException:
            # Don't leave an orphaned .tmp behind on any failure mid-write/replace.
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        self._token_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return token

    def _read_existing_token(self) -> str | None:
        """Return the persisted token, or None when the file is absent or empty."""
        try:
            token = self._token_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return token or None

    def _resolve_binary(self) -> str:
        """Locate the claustrum binary: an explicit config/PATH hit first, else the managed one.

        ``config.claustrum.binary`` wins when it resolves on PATH — an operator who set an absolute
        path or installed claustrum system-wide keeps control. The managed
        ``<state_dir>/deps/bin/claustrum`` (from ``clauster deps install claustrum``) is a fallback
        ONLY for the DEFAULT ``binary`` value: if the operator explicitly configured a *different*
        binary and it doesn't resolve, that's their misconfiguration to see — we must NOT silently
        run a different version than they asked for, so it raises rather than substituting.
        """
        default_binary = type(self._cfg).model_fields["binary"].default
        resolved = deps.resolve_effective_binary(
            "claustrum", self._cfg.binary, default_binary, self._config.state_dir
        )
        if resolved is not None:
            return resolved
        # Record the reason before raising, like every other DaemonSpawnError site: this is
        # the MOST likely real failure (claustrum simply not installed), and without it
        # `/healthz` reported `running:false, error:null` — the reason surviving only in the
        # lifespan log, where a dashboard user never sees it.
        self._error = f"claustrum binary not found: {self._cfg.binary!r} is not on PATH" + (
            " and no managed binary is installed — run `clauster deps install claustrum`"
            if self._cfg.binary == default_binary
            else " (an explicit claustrum.binary must resolve; the managed install is only a "
            "fallback for the default)"
        )
        raise DaemonSpawnError(self._error)

    @staticmethod
    def _unlink_token_handoff(token_file: Path | None) -> None:
        """Best-effort remove the Windows ``-token-file`` when the spawn failed.

        On a healthy start claustrum reads-then-unlinks it during startup; only a
        failed spawn can leave the token on disk, and it must not linger there
        (fail-closed on secrets). A no-op on POSIX, where ``token_file`` is ``None``.
        """
        # no branch: token_file is win32-only (always set on Windows, always None on
        # POSIX), so this guard is one-way per platform — the untaken edge is covered on
        # the *other* OS, never partial in reality.
        if token_file is not None:  # pragma: no branch
            with contextlib.suppress(OSError):
                token_file.unlink()

    async def _connect(self, token: str) -> ClaustrumClient:
        """Dial the socket and verify with an authed ping + capabilities probe.

        The version is read from ``server.capabilities`` (present in every
        claustrum since v1.0.0), not the ``server.version`` method that v1.10
        removes to match the reference daemon. ``capabilities`` keeps the same
        ``version`` field, so this stays forward- and backward-compatible.
        """
        client = ClaustrumClient(
            str(self._socket),
            token,
            request_timeout=self._cfg.request_timeout_seconds,
        )
        await client.connect()
        try:
            await client.ping()
            caps = await client.capabilities()
        except BaseException:
            await client.close()
            raise
        self._version = caps.get("version") if isinstance(caps, dict) else None
        return client

    async def _spawn(self, token: str, deadline: float) -> None:
        """Launch ``claustrum -serve``, handing over the auth token out-of-band.

        POSIX feeds the token over the launcher's stdin (``-token-fd 0``); Windows has
        no usable token fd for the Go daemon, so it writes the token to a short-lived
        ``-token-file`` that claustrum reads-then-unlinks. The launcher re-execs a
        detached child (reparented to ``init`` on POSIX; ``DETACHED_PROCESS`` on
        Windows) and exits 0; the child opens the socket (plus the ``-listen-pipe``
        named pipe on Windows). We wait only for the launcher to exit (until
        ``deadline``), then poll the transport separately — the daemon's own
        stdout/stderr go to ``daemon.log`` so the detached, long-lived child never
        blocks on a pipe we'd drain.
        """
        binary = self._resolve_binary()
        # -keep-children (CL-8): a daemon restart/upgrade then leaves hosted child
        # sessions running for Clauster to reattach/recover. POSIX-only — the daemon
        # ignores it with a warning on Windows.
        argv = [binary, "-serve", "-socket", str(self._socket)]
        if self._cfg.keep_children:
            argv.append("-keep-children")
        # claustrum self-daemonizes (reparented to init on POSIX, DETACHED_PROCESS on
        # Windows) in its own re-exec, so the short-lived launcher Clauster spawns only
        # hands over the token and exits 0 — no creationflags are needed here.
        spawn_kwargs: dict[str, Any] = {}
        token_file: Path | None = None
        if sys.platform == "win32":
            # The Windows client dials a named pipe, not the AF_UNIX socket, so the
            # daemon must also open a pipe listener and advertise it via rpc.pipe (#894).
            argv.append("-listen-pipe")
            # A numeric fd is not a usable token handle for the Go daemon on Windows
            # (fd 0 → "read token-fd: The handle is invalid."), so hand the token over a
            # short-lived file that claustrum reads-then-unlinks (-token-file) rather
            # than -token-fd 0 over stdin. The file is written under the spawn guard
            # below so a write failure is cleaned up + fail-closed, never left on disk.
            token_file = self._socket.parent / f"token-handoff.{secrets.token_hex(8)}.tmp"
            argv += ["-token-file", str(token_file)]
        else:  # pragma: skip-on-win
            # POSIX: feed the token over the launcher's stdin (fd 0) — nothing on disk.
            argv += ["-token-fd", "0"]
            # POSIX-only detach into a new session (setsid); omitted entirely on Windows
            # so no POSIX-only kwarg reaches the Windows subprocess layer at all.
            spawn_kwargs["start_new_session"] = True
        # Defensive: scrub claustrum's daemonize sentinel from the child env (see
        # _DAEMON_SENTINEL_ENV) so an ambient CLAUDE_SSH_DAEMON_CHILD can't make the
        # launcher mistake itself for its own re-exec'd child and skip the token read.
        env = procutil.child_env()  # scrub Clauster secrets; the daemon spawns hosted agents
        for sentinel in _DAEMON_SENTINEL_ENV:
            env.pop(sentinel, None)
        # Append-mode log: the detached daemon keeps writing here after we return.
        log_file = open(self._log_path, "ab")  # noqa: SIM115 - handed to the child; closed below
        try:
            # Windows: write the token handoff here, inside the guard, so a write failure
            # (bad dir / perms / disk full) is cleaned up + surfaced, not leaked on disk.
            # no branch: win32-only guard (see _unlink_token_handoff) — one-way per platform.
            if token_file is not None:  # pragma: no branch
                token_file.write_text(token, encoding="utf-8")
            proc = await asyncio.create_subprocess_exec(
                *argv,
                # POSIX feeds the token over stdin; Windows uses -token-file, so its
                # launcher needs no stdin pipe.
                stdin=asyncio.subprocess.DEVNULL if token_file else asyncio.subprocess.PIPE,
                stdout=log_file,
                stderr=log_file,
                env=env,
                **spawn_kwargs,
            )
        except OSError as exc:
            log_file.close()
            self._unlink_token_handoff(token_file)
            self._error = f"could not launch claustrum: {exc}"
            raise DaemonSpawnError(self._error) from exc

        if (
            token_file is None
        ):  # pragma: skip-on-win  # POSIX: hand the token to the launcher over its stdin pipe.
            stdin = proc.stdin
            if stdin is None:
                log_file.close()
                self._error = "claustrum launcher exposes no stdin pipe"
                raise DaemonSpawnError(self._error)
            try:
                stdin.write(token.encode("utf-8") + b"\n")
                await stdin.drain()
                stdin.close()
            except (
                OSError,
                ConnectionError,
            ):  # non-fatal: a real failure still surfaces via the returncode check below
                # The launcher may have already read its token and closed fd 0; any
                # real failure surfaces via the returncode / poll below.
                logger.debug("claustrum: writing token to launcher stdin failed (already closed?)")

        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        try:
            returncode = await asyncio.wait_for(proc.wait(), timeout=remaining)
        except TimeoutError as exc:
            # The launcher is a `.cmd`/shim → python → detached-child chain on Windows, where
            # `kill()` reaps only the process we hold, so a launcher that timed out mid-detach
            # would leave that chain running with our log file open. Read the blast radius
            # literally: `force_kill_tree` walks `children(recursive=True)`, so this also kills
            # a daemon the launcher DID detach before hanging (DETACHED_PROCESS doesn't erase
            # the ppid link psutil walks) AND its hosted subtree — and because that daemon binds
            # the SHARED socket, another Clauster process that connected in this window loses its
            # sessions too (`self._lock` serialises `ensure()` in-process only). Intended and
            # bounded by `spawn_timeout_seconds`: we declared this launch failed, so we don't
            # leave an unowned daemon serving from it. Best-effort — a failed reap must not mask
            # the DaemonSpawnError this path exists to raise.
            #
            # ⚠️ Deliberately SYNCHRONOUS — do NOT "harmonise" this to `asyncio.to_thread`
            # to match runner.py. `proc` here is an `asyncio.subprocess.Process`, and an
            # await between the reap and `proc.kill()` lets the loop run the child watcher,
            # which nulls the transport's `_proc`; the unguarded `kill()` below would then
            # raise `ProcessLookupError` and skip `_unlink_token_handoff`, leaving the auth
            # token on disk. With no await point that interleaving cannot happen. The psutil
            # walk is one bulk snapshot (single-digit ms) on a path that only runs when a
            # spawn already timed out, and this handler does synchronous FS work regardless.
            if procutil.is_windows():
                try:
                    procutil.force_kill_tree(proc.pid)
                except Exception as tree_exc:  # noqa: BLE001 — never mask the spawn failure
                    logger.debug(
                        "claustrum: tree kill of launcher %s failed: %s", proc.pid, tree_exc
                    )
            proc.kill()
            self._unlink_token_handoff(token_file)
            self._error = "claustrum -serve did not detach within the spawn timeout"
            raise DaemonSpawnError(self._error) from exc
        finally:
            log_file.close()

        if returncode != 0:
            self._unlink_token_handoff(token_file)
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
