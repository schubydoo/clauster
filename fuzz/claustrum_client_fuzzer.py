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

import base64
import sys

import atheris

with atheris.instrument_imports():
    from clauster.claustrum_client import ClaustrumClient, ProcessStream


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)

    # Path 1: a raw daemon line straight into the demux (json parse + routing).
    # Fed as-is (no forced newline) so the fuzzer explores both newline- and
    # non-newline-terminated framings — the async reader's readline() can hand
    # _dispatch a line either way (e.g. an unterminated tail at EOF). No
    # connection is needed; _dispatch only reads the bytes it is handed.
    client = ClaustrumClient("/unused.sock", "tok")
    client._dispatch(fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 256)))

    # Path 2: a SEQUENCE of stream frames into ONE ProcessStream, so line
    # re-assembly across split frames and the exit-flush path are actually
    # exercised. `data` is valid base64 (what the daemon sends) so _decode
    # succeeds and the buffer/split logic is reached, not short-circuited as
    # undecodable; seq is monotonic so frames aren't dropped by the de-dup guard.
    stream = ProcessStream("p")
    stream.subscribe()
    seq = 0
    for _ in range(fdp.ConsumeIntInRange(1, 5)):
        seq += 1
        kind = fdp.PickValueInList(["stdout", "stderr", "exit", "weird"])
        blob = base64.b64encode(fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 64))).decode("ascii")
        frame = {
            "type": "stream",
            "stream": kind,
            "seq": seq,
            "data": blob,
        }
        if kind == "exit":
            frame["exitCode"] = fdp.ConsumeIntInRange(-128, 255) if fdp.ConsumeBool() else None
        stream.feed(frame)


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
