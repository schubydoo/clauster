# Platform support

Clauster runs on Linux, macOS, and Windows, and the same test suite exercises
all three in CI. Most capabilities behave identically everywhere; the matrix
below is the single source of truth for what works where and which
platform-bound mechanism backs each cell. When a row changes, update it
**here** rather than restating the gap in another doc — the scattered notes
elsewhere point back to this matrix.

Legend: ✓ works · ✗ not available — an honest platform gap, not a defect.
Bracketed numbers point at the [notes](#notes) below.

## Capability matrix

| Capability | Linux | macOS | Windows |
| --- | :---: | :---: | :---: |
| Standard (Server Mode) bridge | ✓ | ✓ | ✓ [1] |
| Interactive Session (PTY) bridge | ✓ | ✓ | ✓ [2] [3] |
| Hosted channel (claustrum) | ✓ | ✓ | ✓ [4] |
| Dashboard `claude` login | ✓ | ✓ | ✓ [5] |
| Config-write CLI (`claude mcp` / `claude plugin`) | ✓ | ✓ | ✓ [6] |
| Per-bridge CPU % / RSS memory [7] | ✓ | ✓ | ✓ |
| Per-bridge disk I/O rate | ✓ | ✗ [8] | ✓ |
| Advisory file locking (config writes + bridge lifecycle) | ✓ | ✓ | ✓ [9] |
| Owner-only file modes (`0o600` / `0o700`) | ✓ | ✓ | ✓ [10] |
| Directory `fsync` (crash durability) | ✓ | ✓ | ✓ [11] |
| Service-unit install (`install-service`) [12] | ✓ | ✓ | ✓ |

## Notes

1. Windows spawns the bridge with `CREATE_NEW_PROCESS_GROUP` and stops it via
   `CTRL_BREAK_EVENT`; POSIX uses `start_new_session` + `SIGINT`.
2. POSIX uses `pty.openpty` + `termios`; Windows drives a **ConPTY** keeper
   via **pywinpty**
   ([#903](https://github.com/schubydoo/clauster/pull/903)), which needs the
   `pty` extra — without it `launch_mode: pty` falls back to Server Mode. The
   extra pulls pywinpty on Windows and pyte everywhere (the live pty-screen
   terminal view needs it on every OS). Install it with
   `pip install 'clauster[pty]'` on a package install, or
   `clauster deps install pty` on the standalone binary — the extra is
   intentionally not bundled there, and `deps install` side-loads it instead
   ([#904](https://github.com/schubydoo/clauster/issues/904); see
   [`clauster deps`](../operations.md#clauster-deps-inspect-and-manage-optional-extras)).
3. **Stopping** an Interactive Session on Windows is a hard kill: the bridge
   lives in the keeper's *separate* ConPTY console, so the graceful
   `CTRL_BREAK` can't reach it — the local process is reaped, but the cloud
   session isn't deregistered (it re-registers on the next launch).
   Linux/macOS get the orderly double-`SIGINT`.
4. Windows dials claustrum's named pipe (discovered via `rpc.pipe`) rather
   than the `AF_UNIX` socket, and clauster spawns the daemon with
   `-listen-pipe` plus a `-token-file` handoff (a numeric token fd isn't
   usable there). Round-trip validated
   ([#902](https://github.com/schubydoo/clauster/pull/902)).
5. Subscription sign-in (`claude auth login`, plain pipes) works everywhere;
   the long-lived `setup-token` flow runs under a POSIX PTY on Linux/macOS
   and a **ConPTY** (pywinpty) on Windows
   ([#905](https://github.com/schubydoo/clauster/issues/905)). Because a
   ConPTY echoes written input back — which a parent can't disable the way
   POSIX `termios` does — the operator-pasted code is registered and redacted
   out of any returned/logged output. Like the Interactive Session, the
   Windows ConPTY path needs the `pty` extra
   (`pip install 'clauster[pty]'`); without it `setup-token` fails closed
   with a clear message (subscription sign-in still works).
6. Routes exercised on the Windows CI cells + VM.
7. `psutil.cpu_times` / `memory_info` on every platform.
8. `psutil` has no per-process `io_counters` on macOS, so a bridge card's
   `disk_read_bps` / `disk_write_bps` fields are blank there.
9. An in-process lock serializes clauster's own concurrent writers on every
   OS; POSIX additionally takes an advisory `fcntl.flock`, which also guards
   *other* clauster processes — the config/`CLAUDE.md` writers, and since
   [#949](https://github.com/schubydoo/clauster/issues/949) the per-project
   spawn/stop/forget/adopt sections, so a headless CLI/MCP writer serializes
   against the live server. Cross-clauster-process serialization therefore
   needs the POSIX flock; a single clauster process is fully covered
   everywhere. Neither lock coordinates with the `claude` CLI (which takes no
   lock) — that's covered by the atomic `os.replace` (no torn files) plus the
   caller's external-edit hash guard.
10. POSIX sets `0700`/`0600` mode bits; Windows sets an equivalent owner-only
    ACL on the state dir via `icacls` (remove inheritance; grant only the
    current user + SYSTEM). The Windows ACL is best-effort: the default
    `state_dir` (under `%USERPROFILE%`) already inherits a user + SYSTEM +
    Administrators ACL, so if `icacls` can't run (absent, a domain/service
    account with no resolvable `USERNAME`, a non-zero exit) clauster logs a
    loud warning and proceeds on the inherited ACL rather than blocking every
    state write. Relocate `state_dir` outside the user profile and you should
    tighten it yourself.
11. POSIX `fsync`s the parent directory after a rename; Windows can't `fsync`
    a directory handle, but NTFS **journals** the metadata so the rename is
    recovered on reboot — equivalent durability via a different mechanism.
12. `ops.render_service_unit` renders a systemd unit on Linux, a launchd
    plist on macOS (`launchd` kind), and a
    [Shawl](https://github.com/mtkennerly/shawl) service-install script on
    Windows (`windows` kind). Windows requires Shawl
    (`clauster deps install shawl`) before running the generated script. The
    generated systemd unit sets `KillMode=process` so live pty bridges
    survive a `systemctl restart` (under systemd's default
    `KillMode=control-group` they would be reaped — `clauster doctor` warns
    when an installed unit is set that way); `KillMode` is a systemd concept,
    so this applies on Linux only — launchd and the Windows service have no
    equivalent.
