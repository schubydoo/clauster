"""Legacy JSON store for hosted-channel sessions (CL-6 — reattach across restarts).

Superseded by the SQLite persistence layer (#362/#796): the live store is
:class:`clauster.db.stores.HostedStateStore`, and this module's only consumer in
``src/`` is :func:`clauster.db.bootstrap.import_legacy_json`, which reads the file
once to migrate it. Kept for that one-way import; nothing writes through it now.

A small, separate sibling of :mod:`clauster.state`: the bridge ``state.json`` is
project-keyed (one record per project), but hosted sessions live in their own
:class:`~clauster.hosted.HostedManager` registry, keyed by the client-chosen
``claustrum_process_id`` with possibly several per project. Rather than bend the
bridge schema, hosted state gets its own ``hosted_state.json`` — the same
tolerant-load + atomic-write + schema-versioned posture, keyed by process id.

Like the bridge store this is small and non-authoritative: it holds only what a
restart can't re-derive (the process id to reattach to, the display metadata, and
a best-effort ``daemon_last_seq`` replay cursor). Corruption degrades to "forget
the hosted sessions", never a crash — the daemon's own replay buffer and frame
de-duplication make a stale or missing cursor cost only replay overlap.
"""

from __future__ import annotations

import logging

from .state import KeyedJsonStore

CURRENT_SCHEMA = 1

# The hosted-session fields a restart can't recover live. ``daemon_last_seq`` is
# the reattach replay cursor; ``instance_id`` is the per-runtime UUID lifecycle
# routes also accept (#834/#840) — persisting it lets a client-cached id keep
# resolving across a restart instead of resolving to a freshly-minted one (#841).
# The rest rebuild the dashboard row. All JSON-safe (the manager serializes
# Path/datetime before handing them here).
_PERSISTED_FIELDS = (
    "project",
    "label",
    "permission_mode",
    "claude_session_uuid",
    "daemon_last_seq",
    "hosted_log_path",
    "agent_pid",
    "agent_proc_start",
    # The drift-immune half of the agent pair (#1404). Listed to keep this whitelist the
    # exact mirror of ``db.stores._HOSTED_FIELDS`` — that mirror is the property stopping a
    # field from being persisted by one store and silently dropped by the other. ⚠️ Not
    # dead weight to prune: ``save`` filters on this tuple, and the hosted tests inject
    # THIS class as the manager's store, so removing the entry makes them exercise a row
    # that lost its ticks. Absent from a record simply stays absent, as every field does.
    "agent_start_ticks",
    # The boot the ticks belong to (#1401), kept in lockstep with ``db.stores._HOSTED_FIELDS``
    # for the same mirror reason as ``agent_start_ticks`` above: a field one store persists and
    # the other drops silently loses the cross-boot defense on the store the tests inject.
    "agent_boot_id",
    "started_at",
    "intentional_stop",
    "instance_id",
)

_log = logging.getLogger("clauster.hosted_state")


class HostedStateStore(KeyedJsonStore):
    """Legacy reader for ``hosted_state.json``, keyed by ``claustrum_process_id``.

    Backed by a single ``hosted_state.json`` under the state dir; reads tolerate a
    missing/corrupt file (degrade to ``{}``) and migrate older schemas in place.
    The record map is JSON-keyed ``"sessions"`` (vs the bridge store's
    ``"instances"``), so older on-disk files keep loading unchanged. Only
    :func:`clauster.db.bootstrap.import_legacy_json` consumes it; the live store is
    :class:`clauster.db.stores.HostedStateStore`.
    """

    FILENAME = "hosted_state.json"
    _MAP_KEY = "sessions"
    _PERSISTED_FIELDS = _PERSISTED_FIELDS
    _SCHEMA = CURRENT_SCHEMA
    _LOG = _log
