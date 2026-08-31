"""Atheris fuzz harness for ``hosted.HostedManager._instance_from_record``.

This is how a hosted session survives a clauster restart: on startup every record in
``hosted_state.json`` is mapped back into a ``RemoteControlInstance`` so the browser can
reattach. The audit on issue 1322 called it the most obvious structural gap in the
harness set — a near-copy of the already-fuzzed ``supervisor._job_from_state``, on the
same "coerce a persisted record back into a model" shape, with the same failure mode: a
single bad field aborts the reattach for **every** session, not just its own.

The docstring names the property this harness is built around — *"inverse of
``_record``"* — so the oracle is a **round trip** rather than a restatement of the
mapper's branches:

    record -> instance -> record' -> instance' -> record''      and record'' == record'

A fixed point after one pass, not after zero, and that is the honest form: the first pass
legitimately normalizes (``"a//b"`` becomes ``Path("a//b")`` becomes ``"a/b"``; a
timestamp becomes a ``datetime`` and comes back in canonical ISO form; an uncoercible
cursor becomes ``0``). What must not happen is a *second* pass changing anything — that
would mean the projection and the mapper disagree about what the record means, which is a
session that mutates a little on every restart.

Three further properties, each a promise ``_coerce_seq`` makes in so many words:

* ``daemon_last_seq`` is an ``int``, never a ``bool``, and never negative — a ``True``
  read as seq 1 would *skip* frame 1, the one direction a replay cursor must not fail in,
  and a negative would make ``_note_replay_gap`` report an eviction range for frames that
  never existed.
* ``intentional_stop`` is a real ``bool``.
* ``instance_id`` is a non-empty ``str``, so a client that cached it before the restart
  still resolves through ``HostedManager._key_for``.

⚠️ **``ValidationError`` used to be caught here as a documented boundary; issue 1343
closed the boundary, so the pin is inverted.** The mapper now degrades a record the
model rejects to ``HostedManager._degraded_row`` and logs it, instead of raising
through ``reattach_all`` and the lifespan — which is what made one hand-edited
``{"project": {}}`` fail clauster's whole boot. A ``ValidationError`` reaching this
oracle is therefore a real finding (the guard narrowed), and it is re-raised as an
assertion so the batch report names it. Every other exception was, and stays,
uncaught — that is the ``TypeError``/``OverflowError``/``ValueError`` class
``_coerce_seq`` exists to close, and the class a future field would reopen.

⚠️ ``instance_id`` is generated as ``str | None`` (or absent) only, deliberately. The
writer is ``_record``, which always projects a ``str``, so a non-string could not arrive
from a record clauster wrote — and clauster owns ``hosted_state.json`` (invariant 5), so
this is not an attacker-controlled field. The mapper assigns it *after* construction and
therefore outside pydantic's validation; since issue 1343 it type-checks the value first,
so a hand-edited state file can no longer put a non-string in the registry. The generator
is left narrow anyway, because widening it would only re-prove that ``isinstance`` check.
"""

import sys

import atheris

with atheris.instrument_imports():
    # In-block per fuzz/README.md. `hosted` pulls in the pydantic models the mapper
    # builds, and `datetime` backs the `fromisoformat` branch the round trip drives.
    import datetime  # noqa: F401  — instrumented for coverage; the target parses timestamps

    import pydantic

    from clauster import hosted

#: A stable process id, so the ``f"hosted:{process_id[:8]}"`` label default is the same on
#: every pass of the round trip and cannot itself explain a difference.
_PROCESS_ID = "proc_01JABCDEFGHJKMNPQRSTVWXYZ"

#: The keys ``_record`` writes. Fuzzing exactly this set keeps the harness on the mapper's
#: real input distribution rather than on arbitrary dicts, which the mapper never sees.
_FIELDS = (
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
)


def _value(fdp: atheris.FuzzedDataProvider) -> object:
    """One persisted field value, across the JSON types a state file can hold."""
    pick = fdp.ConsumeIntInRange(0, 6)
    if pick == 0:
        return fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 40)).decode("utf-8", "replace")
    if pick == 1:
        return fdp.ConsumeInt(fdp.ConsumeIntInRange(1, 12))
    if pick == 2:
        return fdp.ConsumeBool()
    if pick == 3:
        return None
    if pick == 4:
        return fdp.ConsumeFloat()
    return [] if pick == 5 else {}


def _timestamp(fdp: atheris.FuzzedDataProvider) -> str:
    """A well-formed ISO-8601 timestamp, assembled from fuzzed components.

    Built rather than hoped for. ``datetime.fromisoformat`` is the mapper's one real
    parser, and random bytes reach it about never — measured: a dictionary of ISO literals
    moved this harness's edge count not at all, because a token only helps if it lands at
    exactly the offset the field is consumed from. Composing the string here reaches the
    *accept* branch on demand, the same way ``auth_headers_fuzzer`` signs a header so the
    accept path is exercised and not only the reject path random bytes can find.

    ⚠️ Expect **no** ``cov:`` gain from this, and do not read that as it being useless:
    ``fromisoformat`` is implemented in C, which Atheris cannot instrument, so the branch
    is genuinely exercised while contributing no Python edges. Measured directly instead —
    over 4000 generated records, 54 of the 64 that carried a string ``started_at`` and
    built parsed through to a real ``datetime``, and those are the ones whose round trip
    goes ``str -> datetime -> isoformat`` rather than collapsing to ``None``.
    """
    return (
        f"{fdp.ConsumeIntInRange(1, 9999):04d}-{fdp.ConsumeIntInRange(1, 12):02d}"
        f"-{fdp.ConsumeIntInRange(1, 31):02d}T{fdp.ConsumeIntInRange(0, 23):02d}"
        f":{fdp.ConsumeIntInRange(0, 59):02d}:{fdp.ConsumeIntInRange(0, 59):02d}"
        f".{fdp.ConsumeIntInRange(0, 999999):06d}"
    )


def _build_record(fdp: atheris.FuzzedDataProvider) -> dict:
    """A persisted record: a subset of ``_record``'s keys, each with an arbitrary value."""
    record: dict = {}
    for field in _FIELDS:
        if not fdp.ConsumeBool():
            continue
        if field == "started_at" and fdp.ConsumeBool():
            record[field] = _timestamp(fdp)
        elif field == "daemon_last_seq" and fdp.ConsumeBool():
            # Digit text, so `int(value or 0)` reaches its success branch as well as the
            # TypeError/ValueError/OverflowError fallbacks `_value` already covers.
            record[field] = str(fdp.ConsumeInt(fdp.ConsumeIntInRange(1, 10)))
        else:
            record[field] = _value(fdp)
    if fdp.ConsumeBool():
        # str | None only — see the module docstring on why this field is not fuzzed wide.
        record["instance_id"] = (
            fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 24)).decode("utf-8", "replace")
            if fdp.ConsumeBool()
            else None
        )
    return record


def check(record: dict) -> None:
    """Assert every property above for one persisted record.

    Split out of :func:`TestOneInput` so ``tests/test_fuzz_harness_smoke.py`` can drive
    the oracle from the suite: fuzz/README.md asks for proof that an assertion *can*
    fire, and an oracle that has never fired is indistinguishable from no oracle at all.
    """
    manager = hosted.HostedManager
    try:
        instance = manager._instance_from_record(_PROCESS_ID, record)
    except pydantic.ValidationError as exc:
        # No longer a boundary (issue 1343) — the mapper is total over the record map.
        raise AssertionError(f"ValidationError escaped the mapper: {exc}") from exc

    seq = instance.daemon_last_seq
    assert isinstance(seq, int) and not isinstance(seq, bool), f"seq not an int: {seq!r}"
    assert seq >= 0, f"negative replay cursor: {seq!r}"
    assert isinstance(instance.intentional_stop, bool), f"{instance.intentional_stop!r}"
    assert isinstance(instance.instance_id, str) and instance.instance_id, (
        f"instance_id is not a non-empty str: {instance.instance_id!r}"
    )

    # The documented inverse relationship, as a fixed point from the first pass onward.
    once = manager._record(instance)
    twice = manager._record(manager._instance_from_record(_PROCESS_ID, once))
    assert twice == once, f"record round trip is not stable\n  1st {once!r}\n  2nd {twice!r}"


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    check(_build_record(fdp))


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
