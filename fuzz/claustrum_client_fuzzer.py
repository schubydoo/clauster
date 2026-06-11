"""Atheris fuzz harness for the claustrum client's daemon-output parsing.

The claustrum daemon's stream notifications carry the spawned agent's stdout
verbatim, so a daemon frame is attacker-influenceable program output that the
client must parse without ever raising. Two entry points handle it:

* ``ClaustrumClient._dispatch`` — one raw NDJSON line from the socket: JSON
  parse, then demux (stream notification vs id-correlated response).
* ``ProcessStream.feed`` — a stream frame: base64-decode ``data`` and reassemble
  newline-delimited lines across split 32 KiB frames.

Both must tolerate *any* bytes/shape (they parse untrusted input), so any
exception this harness surfaces is a real bug — the sibling of the bridge-log
parser invariant in ``parse_markers_fuzzer``.
"""

import sys

import atheris

with atheris.instrument_imports():
    from clauster.claustrum_client import ClaustrumClient, ProcessStream


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)

    # Path 1: a raw daemon line straight into the demux (json parse + routing).
    # No connection is needed — _dispatch only reads the bytes it is handed.
    raw = fdp.ConsumeBytes(fdp.remaining_bytes() // 2)
    client = ClaustrumClient("/unused.sock", "tok")
    client._dispatch(raw + b"\n")

    # Path 2: a structured stream frame straight into line re-assembly, with
    # fuzzer-chosen field types/values the JSON text path may not reach (e.g. a
    # huge seq, an undecodable data blob, an unknown stream kind).
    stream = ProcessStream("p")
    stream.subscribe()
    frame = {
        "type": "stream",
        "stream": fdp.PickValueInList(["stdout", "stderr", "exit", "weird"]),
        "seq": fdp.ConsumeInt(4),
        "data": fdp.ConsumeUnicodeNoSurrogates(fdp.remaining_bytes()),
    }
    stream.feed(frame)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
