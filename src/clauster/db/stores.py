"""DB-backed stores: ``StateStore`` / ``HostedStateStore`` (#362) + history (#363).

:class:`StateStore` and :class:`HostedStateStore` are drop-in replacements for the
JSON stores in :mod:`clauster.state` and :mod:`clauster.hosted_state`: identical
``load() -> dict[str, dict]`` and ``save(records)`` signatures, so
:mod:`clauster.runner` and :mod:`clauster.hosted` are unchanged. The callers
already wrap both in ``asyncio.to_thread``, so a synchronous DB API never blocks
the event loop.

Contract preserved from the JSON stores:

* ``load`` returns ``{key: {persisted fields present for that key}}`` — a field
  absent in the row stays absent in the dict (the JSON store dropped ``None`` /
  unknown keys; callers ``.get(...)`` with defaults).
* ``save`` is a *full replace* of the map: the callers compute the complete subset
  each round, so a key gone from ``records`` must be deleted from the table.
* Fail-closed: a corrupt/unreadable store degrades ``load`` to ``{}`` (never
  crash). ``save`` re-raises as ``OSError`` so the callers' existing best-effort
  ``except OSError`` path (a stale cursor, not a failed spawn) still applies.

:class:`SessionHistoryStore` (#363) is the append-only session-event history: it
appends a lifecycle row, reads history per-project or globally, and derives a
per-project "last used / total cost" rollup. It follows the same fail-closed read
posture — every read degrades to empty on a DB error and never crashes a poll —
while ``append`` is best-effort and swallows write errors (history is
non-authoritative; a lost event row must never fail a spawn or stop).

Since issue 777 the ``StateStore`` is keyed by ``instance_id`` (not project name):
``load`` returns ``{instance_id: {persisted fields including project_name}}`` and
``save`` takes the same shape.  The ``project_name`` field is always present so the
runner can rebuild the per-project index after a restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from ..auth import mint_token
from .models import ApiToken, HostedSession, Instance, Project, SessionEvent

_log = logging.getLogger("clauster.db.stores")

# Terminal lifecycle kinds — the rows that carry the end-of-session cost snapshot.
_TERMINAL_KINDS = ("ended", "crashed")

# Max projects per batched IN() list. SQLite caps host parameters at 999
# (SQLITE_MAX_VARIABLE_NUMBER), so chunk below that to keep the sort working — and
# never silently degrade to empty — on a deployment with very many projects.
_SORTMETA_CHUNK = 900

# The fields each store round-trips, in the order the JSON ``_PERSISTED_FIELDS``
# whitelist listed them. Kept here so the DB store drops the same unknown keys.
# ``instance_id`` and ``project_name`` are the keys, not data fields, but they
# are still included in the persisted payload so the runner can restore both after
# a restart without querying a secondary index.
_INSTANCE_FIELDS = (
    "project_name",
    "label",
    "intentional_stop",
    "spawn_mode",
    "permission_mode",
    "resume_mode",
    # Liveness identity (#1088/#1091) — see the Instance model. ``_present`` drops NULLs, so a
    # row from an older build simply omits these and the reattach path falls back to the
    # pointer/sidecar lookup, exactly as it behaved before the columns existed.
    "bridge_pid",
    "bridge_proc_start",
    "keeper_pid",
)
# The mutable-payload subset: ``project_name`` is the non-null FK, set explicitly in
# _sync (guarded so a record that omits it can't blank an existing row's parent), so
# it is NOT in this loop-set. The rest are nullable and mirrored straight from the record.
_INSTANCE_PAYLOAD_FIELDS = tuple(f for f in _INSTANCE_FIELDS if f != "project_name")
_HOSTED_FIELDS = (
    "project",
    "label",
    "permission_mode",
    "claude_session_uuid",
    "daemon_last_seq",
    "hosted_log_path",
    "agent_pid",
    "agent_proc_start",
    "started_at",
    "intentional_stop",
    "instance_id",
)


def _present(row: object, fields: tuple[str, ...]) -> dict:
    """Return ``{field: value}`` for non-``None`` columns of ``row``.

    Mirrors the JSON store's drop-absent behaviour: a column left ``NULL`` is
    omitted entirely, so a caller's ``.get(field)`` sees ``None`` exactly as it did
    when the JSON file simply didn't carry the key.
    """
    out: dict = {}
    for field in fields:
        value = getattr(row, field)
        if value is not None:
            out[field] = value
    return out


class StateStore:
    """Per-instance bridge intent, backed by the ``instances`` table (#777).

    Keyed by ``instance_id`` (a stable UUID minted at spawn time).  Each record
    carries ``project_name`` as a data field so the runner can rebuild the
    per-project index after a restart without a secondary index.

    Same contract as :class:`clauster.state.StateStore`:
    ``load() → {instance_id: {persisted fields}}`` and
    ``save({instance_id: {fields}})`` — fail-closed on read.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        """Bind the store to a session factory (the shared engine's ``sessionmaker``)."""
        self._sessions = session_factory

    def load(self) -> dict[str, dict]:
        """Return ``{instance_id: {persisted fields}}``; ``{}`` on any DB error."""
        try:
            with self._sessions() as session:
                rows = session.execute(select(Instance)).scalars().all()
                return {row.instance_id: _present(row, _INSTANCE_FIELDS) for row in rows}
        except SQLAlchemyError as exc:
            # Fail-closed like the JSON store's corrupt-file path: a read failure
            # degrades to "forget the labels", never a crash on startup.
            _log.warning("state load failed, degrading to empty: %s", exc)
            return {}

    def load_strict(self) -> dict[str, dict]:
        """Like :meth:`load`, but raise :class:`OSError` on a DB error instead of ``{}``.

        For callers that REFRESH an already-loaded map (#949): degrading a transient
        read failure to ``{}`` there would replace a known-good merge base with an
        empty one, and the next full-replace :meth:`save` would prune every row the
        caller isn't currently tracking. Raising lets the caller keep its previous
        base (a stale cursor, never a mass delete). ``OSError`` mirrors :meth:`save`'s
        error translation so callers handle one exception type.
        """
        try:
            with self._sessions() as session:
                rows = session.execute(select(Instance)).scalars().all()
                return {row.instance_id: _present(row, _INSTANCE_FIELDS) for row in rows}
        except SQLAlchemyError as exc:
            raise OSError(f"state load failed: {exc}") from exc

    def save(self, records: dict[str, dict]) -> None:
        """Replace the ``instances`` map with ``records`` (full upsert + prune).

        ``records`` is ``{instance_id: {fields}}``; each record must carry a
        ``project_name`` key so the FK parent can be ensured. Raises
        :class:`OSError` on failure so the callers' best-effort ``except OSError``
        still applies.
        """
        try:
            with self._sessions() as session:
                with session.begin():
                    self._sync(session, records)
        except SQLAlchemyError as exc:
            raise OSError(f"state save failed: {exc}") from exc

    @staticmethod
    def _sync(session: Session, records: dict[str, dict]) -> None:
        """Upsert every record and delete instance rows absent from ``records``."""
        keep = set(records)
        existing = {
            row.instance_id: row for row in session.execute(select(Instance)).scalars().all()
        }
        known_projects = set(session.execute(select(Project.name)).scalars().all())
        for instance_id, fields in records.items():
            project_name = fields.get("project_name", "")
            if project_name and project_name not in known_projects:
                session.add(Project(name=project_name))
                known_projects.add(project_name)
            row = existing.get(instance_id)
            if row is None:
                row = Instance(instance_id=instance_id, project_name=project_name)
                session.add(row)
            else:
                # project_name may change on import/migration; keep it current.
                # Guarded: a record that omits it must NOT blank the non-null FK
                # (the setattr loop below no longer touches project_name).
                if project_name:
                    row.project_name = project_name
            for field in _INSTANCE_PAYLOAD_FIELDS:
                setattr(row, field, fields.get(field))
        for iid, row in existing.items():
            if iid not in keep:
                session.delete(row)


class HostedStateStore:
    """Hosted-channel sessions, backed by the ``hosted_sessions`` table.

    Same contract as :class:`clauster.hosted_state.HostedStateStore`: keyed by
    ``claustrum_process_id``, round-trips the reattach metadata + cursor,
    fail-closed on read.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        """Bind the store to a session factory (the shared engine's ``sessionmaker``)."""
        self._sessions = session_factory

    def load(self) -> dict[str, dict]:
        """Return ``{process_id: {persisted fields}}``; ``{}`` on any DB error."""
        try:
            with self._sessions() as session:
                rows = session.execute(select(HostedSession)).scalars().all()
                return {row.claustrum_process_id: _present(row, _HOSTED_FIELDS) for row in rows}
        except SQLAlchemyError as exc:
            _log.warning("hosted-state load failed, degrading to empty: %s", exc)
            return {}

    def save(self, records: dict[str, dict]) -> None:
        """Replace the ``hosted_sessions`` map with ``records`` (full upsert + prune)."""
        try:
            with self._sessions() as session:
                with session.begin():
                    self._sync(session, records)
        except SQLAlchemyError as exc:
            raise OSError(f"hosted-state save failed: {exc}") from exc

    @staticmethod
    def _sync(session: Session, records: dict[str, dict]) -> None:
        """Upsert every record and delete hosted rows absent from ``records``."""
        keep = set(records)
        existing = {
            row.claustrum_process_id: row
            for row in session.execute(select(HostedSession)).scalars().all()
        }
        for process_id, fields in records.items():
            row = existing.get(process_id)
            if row is None:
                row = HostedSession(claustrum_process_id=process_id)
                session.add(row)
            for field in _HOSTED_FIELDS:
                setattr(row, field, fields.get(field))
        gone = [pid for pid in existing if pid not in keep]
        if gone:
            session.execute(
                delete(HostedSession).where(HostedSession.claustrum_process_id.in_(gone))
            )


@dataclass(frozen=True)
class CostSnapshot:
    """The end-of-session cost/token cluster carried on a history row.

    A single value object for the five fields that used to be spelled out on every
    history DTO (:class:`HistoryEvent`, :class:`ProjectRollup`, the store's write
    signature). All are ``None`` until a terminal (``ended`` / ``crashed``) row
    records them, so a default :class:`CostSnapshot` is the "no totals yet" state.
    """

    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cache_read_tokens: int | None = None

    @classmethod
    def from_row(cls, row: SessionEvent) -> CostSnapshot:
        """Read the cost/token columns off a persisted row into a snapshot."""
        return cls(
            cost_usd=row.cost_usd,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            cache_creation_tokens=row.cache_creation_tokens,
            cache_read_tokens=row.cache_read_tokens,
        )


# The "no totals yet" snapshot — shared as the default for pre-terminal rows and the
# store's write signature (a frozen instance is immutable, so one singleton is safe and
# keeps it out of dataclass-field / argument-default call positions).
_NO_COST = CostSnapshot()


@dataclass(frozen=True)
class HistoryEvent:
    """One session-history row as the read API hands it out (a plain value object).

    A read-only snapshot of a :class:`~clauster.db.models.SessionEvent` row. The
    :attr:`cost` totals are populated only on a terminal (``ended`` / ``crashed``)
    row; they are all ``None`` on ``spawned`` / ``ready`` rows.
    """

    id: int
    project_name: str
    mode: str
    kind: str
    at: datetime
    session_ref: str | None = None
    cost: CostSnapshot = _NO_COST


@dataclass(frozen=True)
class ProjectRollup:
    """Per-project "last used / total cost" derived straight from the history table.

    ``last_used`` is the most recent event timestamp for the project (``None`` when
    it has no history). :attr:`cost` is the cumulative end-of-session snapshot from
    the project's most recent terminal row — the same ballpark figure
    :mod:`clauster.usage` produces — with all-``None`` fields when no terminal row
    has been recorded yet. ``event_count`` is the project's total row count.
    """

    project_name: str
    last_used: datetime | None = None
    event_count: int = 0
    cost: CostSnapshot = _NO_COST


def _to_event(row: SessionEvent) -> HistoryEvent:
    """Map a persisted row to the read API's value object."""
    return HistoryEvent(
        id=row.id,
        project_name=row.project_name,
        mode=row.mode,
        kind=row.kind,
        at=row.at,
        session_ref=row.session_ref,
        cost=CostSnapshot.from_row(row),
    )


class SessionHistoryStore:
    """Append-only session lifecycle / event history, backed by ``session_events`` (#363).

    Records one row per ``spawned`` / ``ready`` / ``ended`` / ``crashed`` transition
    and serves the per-project + global history reads the Projects-zone "last used"
    sort (#298) and the pty resume picker (#303) build on. Reads are fail-closed
    (degrade to empty on a DB error, never crash a poll); :meth:`append` is
    best-effort and swallows write errors — history is non-authoritative, so a lost
    event row must never fail a spawn or stop.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        """Bind the store to a session factory (the shared engine's ``sessionmaker``)."""
        self._sessions = session_factory

    def append(
        self,
        *,
        project_name: str,
        mode: str,
        kind: str,
        at: datetime | None = None,
        session_ref: str | None = None,
        cost: CostSnapshot = _NO_COST,
    ) -> bool:
        """Append one lifecycle event; return whether it was written.

        Ensures a parent :class:`~clauster.db.models.Project` row exists (the FK
        target, mirroring :meth:`StateStore._sync`) before inserting. Best-effort:
        on any DB error the failure is logged and ``False`` returned — a lost
        history row must never break the bridge lifecycle. The ``cost`` totals are
        only meaningful on a terminal (``ended`` / ``crashed``) row.
        """
        try:
            with self._sessions() as session, session.begin():
                if not session.get(Project, project_name):
                    session.add(Project(name=project_name))
                session.add(
                    SessionEvent(
                        project_name=project_name,
                        mode=mode,
                        kind=kind,
                        at=at if at is not None else datetime.now(tz=UTC),
                        session_ref=session_ref,
                        cost_usd=cost.cost_usd,
                        input_tokens=cost.input_tokens,
                        output_tokens=cost.output_tokens,
                        cache_creation_tokens=cost.cache_creation_tokens,
                        cache_read_tokens=cost.cache_read_tokens,
                    )
                )
            return True
        except SQLAlchemyError as exc:
            _log.warning("could not record session event (%s/%s): %s", project_name, kind, exc)
            return False

    def history_for(self, project_name: str, *, limit: int | None = None) -> list[HistoryEvent]:
        """Return a project's events, newest first; ``[]`` on any DB error.

        ``limit`` caps the number of rows returned (the most recent ``limit``).
        Served by the ``(project_name, at)`` composite index.
        """
        try:
            with self._sessions() as session:
                stmt = (
                    select(SessionEvent)
                    .where(SessionEvent.project_name == project_name)
                    .order_by(SessionEvent.at.desc(), SessionEvent.id.desc())
                )
                if limit is not None:
                    stmt = stmt.limit(limit)
                return [_to_event(row) for row in session.execute(stmt).scalars().all()]
        except SQLAlchemyError as exc:
            _log.warning(
                "session history read failed for %s, degrading to empty: %s", project_name, exc
            )
            return []

    def history(self, *, limit: int | None = None) -> list[HistoryEvent]:
        """Return events across all projects, newest first; ``[]`` on any DB error.

        ``limit`` caps the number of rows returned (the most recent ``limit``).
        Served by the standalone ``at`` index.
        """
        try:
            with self._sessions() as session:
                stmt = select(SessionEvent).order_by(
                    SessionEvent.at.desc(), SessionEvent.id.desc()
                )
                if limit is not None:
                    stmt = stmt.limit(limit)
                return [_to_event(row) for row in session.execute(stmt).scalars().all()]
        except SQLAlchemyError as exc:
            _log.warning("global session history read failed, degrading to empty: %s", exc)
            return []

    def rollup_for(self, project_name: str) -> ProjectRollup:
        """Return a project's "last used / total cost" rollup; empty on a DB error.

        ``last_used`` is the project's most recent event timestamp. The :attr:`cost`
        totals come from the project's most recent terminal (``ended`` / ``crashed``)
        row, which carries the cumulative end-of-session snapshot. A project with no
        history (or on a DB error) yields a rollup with ``last_used=None`` and an
        empty :class:`CostSnapshot` — never a crash.
        """
        try:
            with self._sessions() as session:
                last_used = session.execute(
                    select(func.max(SessionEvent.at)).where(
                        SessionEvent.project_name == project_name
                    )
                ).scalar_one()
                count = session.execute(
                    select(func.count())
                    .select_from(SessionEvent)
                    .where(SessionEvent.project_name == project_name)
                ).scalar_one()
                terminal = session.execute(
                    select(SessionEvent)
                    .where(
                        SessionEvent.project_name == project_name,
                        SessionEvent.kind.in_(_TERMINAL_KINDS),
                    )
                    .order_by(SessionEvent.at.desc(), SessionEvent.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
                return ProjectRollup(
                    project_name=project_name,
                    last_used=last_used,
                    event_count=count,
                    cost=CostSnapshot.from_row(terminal) if terminal else _NO_COST,
                )
        except SQLAlchemyError as exc:
            _log.warning(
                "session rollup read failed for %s, degrading to empty: %s", project_name, exc
            )
            return ProjectRollup(project_name=project_name)

    def sortmeta_for_all(
        self, names: list[str]
    ) -> dict[str, tuple[datetime | None, float | None]]:
        """Return ``{name: (last_used, cost_usd)}`` for many projects in ONE session.

        The batched form of :meth:`rollup_for` for the Projects sort control, which
        needs only ``last_used`` and the most-recent-terminal ``cost_usd``. Two grouped
        queries replace ``rollup_for``'s per-project 3-SELECT loop (the dashboard-sort
        N+1: P projects × a session checkout × 3 SELECTs). The ``IN()`` lists are
        chunked at :data:`_SORTMETA_CHUNK` to stay under SQLite's host-parameter cap.
        Names with no history are omitted (the caller defaults them to ``(None, None)``).
        Degrades to ``{}`` on any DB error — a sort never crashes the dashboard.
        """
        if not names:
            return {}
        try:
            out: dict[str, tuple[datetime | None, float | None]] = {}
            with self._sessions() as session:
                for start in range(0, len(names), _SORTMETA_CHUNK):
                    chunk = names[start : start + _SORTMETA_CHUNK]
                    for project_name, last_used in session.execute(
                        select(SessionEvent.project_name, func.max(SessionEvent.at))
                        .where(SessionEvent.project_name.in_(chunk))
                        .group_by(SessionEvent.project_name)
                    ):
                        out[project_name] = (last_used, None)
                    # Most-recent terminal row per project (the cumulative cost snapshot),
                    # picked with one windowed pass instead of a per-project LIMIT 1 query.
                    ranked = (
                        select(
                            SessionEvent.project_name.label("project_name"),
                            SessionEvent.cost_usd.label("cost_usd"),
                            func.row_number()
                            .over(
                                partition_by=SessionEvent.project_name,
                                order_by=(SessionEvent.at.desc(), SessionEvent.id.desc()),
                            )
                            .label("rn"),
                        )
                        .where(
                            SessionEvent.project_name.in_(chunk),
                            SessionEvent.kind.in_(_TERMINAL_KINDS),
                        )
                        .subquery()
                    )
                    for project_name, cost_usd in session.execute(
                        select(ranked.c.project_name, ranked.c.cost_usd).where(ranked.c.rn == 1)
                    ):
                        prior = out.get(project_name, (None, None))
                        out[project_name] = (prior[0], cost_usd)
            return out
        except SQLAlchemyError as exc:
            _log.warning("batch sortmeta read failed, degrading to empty: %s", exc)
            return {}


@dataclass(frozen=True)
class ApiTokenRecord:
    """One named API token as the read API hands it out — never the hash or raw secret.

    ``clauster api-token list`` prints exactly these three fields; the SHA-256
    ``token_hash`` backing the row is an internal verification detail and is
    deliberately not part of this value object.
    """

    label: str
    created_at: datetime
    last_used_at: datetime | None = None


def _to_token_record(row: ApiToken) -> ApiTokenRecord:
    """Map a persisted row to the read API's value object (hash excluded on purpose)."""
    return ApiTokenRecord(
        label=row.label, created_at=row.created_at, last_used_at=row.last_used_at
    )


class ApiTokenStore:
    """Named public-API bearer tokens, backed by the ``api_tokens`` table (#302).

    CLI-first (``clauster api-token issue|list|rotate|revoke``): the running app
    only ever calls :meth:`is_active_hash` (per-request auth check) and
    :meth:`touch_last_used` (best-effort bookkeeping on a successful auth); every
    mutating verb here is driven by the CLI, which owns its own short-lived
    :class:`~clauster.db.persistence.Persistence`.

    Reads fail closed, each in the direction that is safe for it: a DB error denies in
    ``is_active_hash`` (``False``) and RAISES :class:`OSError` in :meth:`list_all` — an
    empty audit list would read as "no bearer tokens exist". ``touch_last_used`` is
    best-effort and swallows write errors — a lost bookkeeping update must never
    fail the request it authenticated.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        """Bind the store to a session factory (the shared engine's ``sessionmaker``)."""
        self._sessions = session_factory

    def list_all(self) -> list[ApiTokenRecord]:
        """Return every named token, oldest first.

        Raises :class:`OSError` on a DB failure (mirrors :meth:`revoke`) — a locked
        or corrupt DB must surface as an error, never degrade to ``[]``: an operator
        auditing tokens would read that as "no bearer tokens exist" while existing
        rows may still authenticate once the DB recovers. Fail closed, never silently.
        """
        try:
            with self._sessions() as session:
                rows = (
                    session.execute(select(ApiToken).order_by(ApiToken.created_at)).scalars().all()
                )
                return [_to_token_record(row) for row in rows]
        except SQLAlchemyError as exc:
            raise OSError(f"api-token list failed: {exc}") from exc

    def issue(self, label: str) -> tuple[str, ApiTokenRecord]:
        """Mint + persist a new named token; return ``(raw_token, record)``.

        The raw token is generated here and returned to the caller exactly once
        (mirrors :func:`clauster.auth.mint_token` everywhere else) — only its hash
        is ever written to the table. Raises :class:`ValueError` if ``label`` is
        already taken (never silently rotates an existing token under a caller's
        back) and :class:`OSError` on any other DB failure.
        """
        raw, token_hash = mint_token()
        try:
            with self._sessions() as session, session.begin():
                existing = session.execute(
                    select(ApiToken.id).where(ApiToken.label == label)
                ).scalar_one_or_none()
                if existing is not None:
                    raise ValueError(f"a token labeled {label!r} already exists")
                row = ApiToken(label=label, token_hash=token_hash)
                session.add(row)
                session.flush()
                record = _to_token_record(row)
        except SQLAlchemyError as exc:
            raise OSError(f"api-token issue failed: {exc}") from exc
        return raw, record

    def rotate(self, label: str) -> tuple[str, ApiTokenRecord]:
        """Mint a fresh secret for an existing label; return ``(raw_token, record)``.

        The label and ``created_at`` are preserved (the identity persists across a
        rotation); ``last_used_at`` resets to ``None`` since the new secret is
        unused. Raises :class:`ValueError` if no token has that label.
        """
        raw, token_hash = mint_token()
        try:
            with self._sessions() as session, session.begin():
                row = session.execute(
                    select(ApiToken).where(ApiToken.label == label)
                ).scalar_one_or_none()
                if row is None:
                    raise ValueError(f"no token labeled {label!r}")
                row.token_hash = token_hash
                row.last_used_at = None
                session.flush()
                record = _to_token_record(row)
        except SQLAlchemyError as exc:
            raise OSError(f"api-token rotate failed: {exc}") from exc
        return raw, record

    def revoke(self, label: str) -> bool:
        """Delete the token labeled ``label``; return whether one was found.

        Raises :class:`OSError` on a DB failure so the CLI never reports a
        successful revoke that didn't actually happen (fail-closed: a caller
        must be able to trust a `True` return).
        """
        try:
            with self._sessions() as session, session.begin():
                result = session.execute(delete(ApiToken).where(ApiToken.label == label))
                # Session.execute is typed Result[Any]; DML statements return a
                # CursorResult at runtime, which is what carries rowcount.
                return cast(CursorResult, result).rowcount > 0
        except SQLAlchemyError as exc:
            raise OSError(f"api-token revoke failed: {exc}") from exc

    def is_active_hash(self, token_hash: str) -> bool:
        """Whether ``token_hash`` matches a currently-issued named token.

        Fail-closed: a DB error denies rather than authenticates. Called on the
        request hot path (via ``asyncio.to_thread``), so this is a single indexed
        equality lookup — no full-table scan.
        """
        try:
            with self._sessions() as session:
                row = session.execute(
                    select(ApiToken.id).where(ApiToken.token_hash == token_hash)
                ).scalar_one_or_none()
                return row is not None
        except SQLAlchemyError as exc:
            _log.warning("api-token verify failed, denying: %s", exc)
            return False

    def touch_last_used(self, token_hash: str) -> None:
        """Best-effort ``last_used_at`` bump for the token matching ``token_hash``.

        Swallows any DB error (logged, not raised) — bookkeeping must never fail
        the request it just authenticated. A no-op if ``token_hash`` matches no
        row (e.g. the legacy ``config.auth.api_token_hash`` path, which has no
        table row to update).
        """
        try:
            with self._sessions() as session, session.begin():
                session.execute(
                    update(ApiToken)
                    .where(ApiToken.token_hash == token_hash)
                    .values(last_used_at=datetime.now(UTC))
                )
        except SQLAlchemyError as exc:
            _log.warning("could not record api-token last-used: %s", exc)
