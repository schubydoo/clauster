"""Atheris fuzz harness for ``supervisor._job_from_state``.

``_job_from_state`` coerces one entry of the Anthropic agent-view daemon's on-disk
state (a version-churning research-preview file) into a ``BackgroundJob``. Every
field is funnelled through ``_clean`` / ``_opt_str`` / ``_opt_path``, so it must be
TOTAL over any JSON object: never raise, and always produce a valid
``BackgroundJob`` (a pydantic ``ValidationError`` would mean a coercion helper
failed its type contract). Driven with ``workers={}`` so it never probes live PIDs.
Any raise is a real bug; the harness catches nothing.
"""

import sys

import atheris

with atheris.instrument_imports():
    from clauster import supervisor


def _value(fdp: atheris.FuzzedDataProvider):
    """A string, int, None, or list — exercises the isinstance / coercion branches."""
    pick = fdp.ConsumeIntInRange(0, 3)
    if pick == 0:
        return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 64))
    if pick == 1:
        return fdp.ConsumeIntInRange(-16, 16)
    if pick == 2:
        return None
    return [fdp.ConsumeIntInRange(0, 8)]


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)

    def text() -> str:
        return fdp.ConsumeUnicodeNoSurrogates(fdp.ConsumeIntInRange(0, 48))

    record = {
        "state": text(),
        "detail": text(),
        "tempo": text(),
        "needs": text(),
        "intent": text(),
        "name": text(),
        "cliVersion": text(),
        "createdAt": text(),
        "updatedAt": text(),
        "cwd": _value(fdp),
        "linkScanPath": _value(fdp),
        "sessionId": _value(fdp),
        "bridgeSessionId": _value(fdp),
    }
    out = fdp.ConsumeIntInRange(0, 2)
    if out == 0:
        record["output"] = {"result": text()}
    elif out == 1:
        record["output"] = {"result": _value(fdp)}
    else:
        record["output"] = _value(fdp)

    # Contract: total over any dict — never raises, always builds a BackgroundJob.
    job = supervisor._job_from_state("fuzzjob0", record, {})
    assert isinstance(job, supervisor.BackgroundJob)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
