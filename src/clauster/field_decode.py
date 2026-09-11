"""Pure coercion/decoding of persisted-row, bridge-pointer, and keeper-sidecar fields.

Extracted from :mod:`clauster.runner` (part of #1157). These stateless helpers turn the
untyped values a persisted ``state.json`` row, an Anthropic ``bridge-pointer.json``, or a
keeper sidecar can carry into the typed, bounded liveness/identity values the reattach and
poll paths need. They live in one low-level module because BOTH the still-on-``runner``
methods (``_persisted_liveness``, the poll-loop promotion) AND the reattach/adopt/rediscover
methods now in :mod:`clauster.rediscovery` call them; keeping them here lets both import the
one copy without a circular import, and :mod:`clauster.runner` re-exports the names the tests
reach as ``clauster.runner._row_int`` etc.
"""

from __future__ import annotations

from . import procutil, redact


def _row_int(value: object) -> int | None:
    """Coerce a persisted-row field to ``int``, or ``None`` if it isn't one (#1088).

    ``bool`` is a subclass of ``int``, so a hand-edited row carrying ``"bridge_pid": true``
    would otherwise resolve to PID 1 and have its liveness checked against init. Excluded,
    matching the convention in ``procutil`` and ``pty_keeper``.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _pointer_start_ticks(proc_start: str | None) -> int | None:
    """Coerce a bridge pointer's ``procStart`` to boot-relative ticks, or ``None`` (#1399).

    A pointer records Linux ``starttime`` jiffies **as a string**, which is already the
    drift-immune quantity ``bridge_start_ticks`` wants — so a pointer-walked or adopted
    bridge can carry the same PID-reuse defense a spawned one gets, instead of being judged
    on the epoch ``_expected_epoch`` derives from it (that conversion bakes in today's
    ``/proc/stat`` btime, which NTP then moves out from under the comparison).

    Not ``_row_int``: that is for persisted-row fields, which hold real ints, and it would
    silently answer ``None`` for every pointer here — a no-op fix that still looked applied.
    """
    try:
        ticks = int(str(proc_start))
    except (TypeError, ValueError):
        return None
    # Bounded, because this value reaches an INTEGER column: a pointer is an untrusted
    # on-disk file (this module already treats it that way), and a value past 2**63 raises
    # OverflowError out of `_persist`, which catches only OSError. A negative one cannot
    # authenticate a wrong process — it can only ever fail the exact compare — but it fails
    # in the #1399 direction, so reject it here rather than record a value that means
    # "permanently not live". The bound is the COLUMN's limit, deliberately, not a guess at a
    # plausible uptime: at CLK_TCK=100 a 2**31 cap starts discarding real tick counts after
    # 248 days up, silently dropping drift protection on exactly the long-lived hosts that
    # have accumulated the most correction — and `_spawn`'s own `proc_start_ticks` stamp is
    # uncapped, so the two halves of one column would disagree about what is valid.
    return ticks if 0 <= ticks < 2**63 else None


def _ticks_on_exact_match(pid: int, proc_start: float | None) -> int | None:
    """Recover a tick-less pair's boot-relative ticks at a moment its epoch matches exactly.

    A row written before ``bridge_start_ticks`` existed carries only the drifting epoch, so
    on Linux every verdict on it is inconclusive (#1399): the prune refuses to delete it,
    but the first mismatch still demotes its card to STOPPED, and ``_reconcile_status``
    never promotes back. Slew oscillates, though, so the epoch keeps returning to an exact
    match (one sample in five on the dogfood host), and each such moment is an
    opportunity to read the drift-immune half and stamp it. The callers persist a stamp
    (startup through ``rediscover``'s final write, the poll loop with its own), after
    which the pair is immune across restarts too.

    Deliberately NOT healed from any other source. A fresh tick read agrees with whatever
    holds the pid by construction, and the project's pointer names whatever bridge is at
    that cwd now, so neither can tell this row's process from a recycled pid on a coarse
    epoch bound (which a same-project bridge on a recycled pid passes) and a wrong guess
    overwrites a resumable record. The exact 0.05s bound is the
    one the code trusted for ``stop`` and ``forget`` before ticks existed, so stamping only
    where it holds makes nothing laxer. ONE read via :func:`procutil.proc_start_pair`, so
    the epoch that matched and the ticks stamped describe the same process.

    ``None`` where the bound does not hold this instant, where ticks are unreadable, and
    where create-time does not drift at all (nothing to heal, and a read there could only
    authenticate a recycled pid). A host whose clock STEPPED once never re-matches, so it
    never heals; that is the residue issue 1401 tracks.
    """
    if proc_start is None or not procutil.start_time_is_drift_prone():
        return None
    epoch, ticks = procutil.proc_start_pair(pid)
    if ticks is None or epoch is None:
        return None
    if abs(epoch - proc_start) > procutil._EXACT_PROC_START_TOLERANCE:
        return None
    return ticks


def _row_float(value: object) -> float | None:
    """Coerce a persisted-row field to ``float``, or ``None`` if it isn't one (#1088).

    A missing or junk ``bridge_proc_start`` degrades to ``None`` — cmdline-only liveness,
    exactly as an unparseable pointer ``procStart`` does — rather than raising out of the
    startup reattach.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


#: Cap on a keeper `note` lifted onto a card. The keeper writes one fixed ~70-character
#: sentence, so this only bounds a hand-edited or corrupt sidecar — kept tight because the
#: value renders inline in the card's button cluster, which wraps: a long string does not
#: overflow but does push the row's controls onto extra lines.
_NOTICE_MAX_CHARS = 120


def _sidecar_notice(info: dict) -> str | None:
    """Read a keeper sidecar's advisory ``note`` as a bounded, redacted string (#1390).

    THE single reader for that field, shared by the three paths that can build a RUNNING pty
    row from a sidecar — :meth:`SessionRunner._apply_pty_info` (spawn + startup watch),
    :meth:`SessionRunner._reattach_pty_from_sidecar` (a live keeper adopted with no row to
    correlate it to), and :meth:`SessionRunner._connect_facts_for` (a persisted row
    reattached after a restart). They show the same card, so a note read on only some of them
    would appear and then vanish.

    :meth:`SessionRunner._connect_facts_for` reads it as one of its returned facts (#1438),
    inside the leg already gated on ``state == "ready"`` — the readiness evidence the spawn
    path promotes on — and UNCONDITIONALLY, so the ``notice`` key (its value or ``None``) is
    always present once that gate passes (#1452). Its three callers
    (:meth:`_reattach_rows_with_pids`, :meth:`_adopt_rows_from_store`,
    :meth:`_promote_ready_unwatched`) use the returned dict's emptiness as the readiness gate,
    so an always-present ``notice`` makes a correlated ready sidecar's dict non-empty even
    with no url, no session id and no note — the row then promotes on the ready state, the
    same as the spawn path, and each caller copies ``notice`` onto the card (``None`` clears a
    stale one). This matters because the keeper reaches ``ready`` two linkless ways: a screen
    fault, which nulls ``connect_url`` and writes a note; and a URL scrape that simply missed,
    which writes neither. Before #1438 this helper skipped the note; before #1452 it lifted
    the note only when present — so a bare ready sidecar returned an empty dict, read as "no
    evidence", and a row rebuilt through it after a restart stayed STARTING for the life of
    the process (the gap #1390 and #1452 named).

    Treated as untrusted text even though the keeper only ever writes a fixed constant: a
    sidecar is an on-disk file a hand edit or a corrupt write can put anything into (the
    same reasoning the pid fields' ``> 0`` gates carry), and this one is rendered on the
    dashboard. Redacted for the same reason ``_capture_error_detail`` redacts — nothing
    reaching the browser skips ``redact`` (invariant 4) — then bounded.

    A non-string, empty or whitespace-only value degrades to ``None`` (no chip) rather than
    an empty advisory the operator cannot read a reason out of.
    """
    note = info.get("note")
    if not isinstance(note, str) or not note.strip():
        return None
    # Head, not tail (unlike `error_detail`'s stderr tail): a note is one sentence written
    # front-first, so a truncation must keep its beginning.
    return redact.redact_for_disk(note)[:_NOTICE_MAX_CHARS]


def _row_str(value: object) -> str | None:
    """Coerce a persisted-row field to ``str``, or ``None`` if it isn't one (#1401).

    ``bridge_boot_id`` is a string in every row this process writes, but a hand-edited row
    can hold junk. A non-string OR empty value degrades to ``None`` — ticks-only liveness,
    exactly as an absent boot id does — rather than reaching the identity compare as the wrong
    type or as an ``""`` that can never match a live boot id (which would read a live bridge as
    dead, the #1399 failure shape). ``proc_boot_id`` maps an empty read to ``None`` too.
    """
    return value if isinstance(value, str) and value else None
