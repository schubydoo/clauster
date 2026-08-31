"""Atheris fuzz harness for ``hosted._redact_obj``.

``_redact_obj`` is where safety invariant 4 is enforced on the hosted channel: every
stream-json frame the claustrum daemon relays — assistant text, tool input, tool
*output* — passes through it on the way to a browser subscriber. The frames carry
whatever a tool printed, so their shape is not ours to choose: arbitrary nesting,
arbitrary keys, arbitrary string leaves.

Its contract is two sentences, and this harness holds it to both:

1. **Every string leaf is sanitized.** Asserted as the property that matters rather than
   as "``sanitize_line`` was called": no bare ``env_``/``session_``/``cse_`` identifier
   survives anywhere in the output. The matcher is restated here rather than imported,
   exactly as ``redact_fuzzer`` restates it — an oracle that asks the redactor whether it
   redacted cannot fail.
2. **Never raises on a deeply-nested frame.** A container past ``_REDACT_MAX_DEPTH`` is
   replaced by the ``_REDACT_TOO_DEEP`` marker rather than recursed into. The harness
   therefore *aims* at that boundary: it builds chains well past the limit on purpose,
   because a frame nested 100 deep is exactly what a hostile tool result would carry, and
   the pre-guard behaviour was a ``RecursionError`` mid-fan-out.

Two structural properties come with it, because a redactor that mangles the frame breaks
the client just as surely as one that leaks:

* **Depth is actually capped** — no container in the output sits deeper than
  ``_REDACT_MAX_DEPTH``. This is the assertion that fails if the guard is off by one or
  is bypassed on one of the two container branches.
* **Shape is preserved above the cap** — dict key sets and list lengths come back
  unchanged, so redaction cannot silently drop or reorder a frame's fields.

⚠️ **Dict *keys* are deliberately out of scope, and this harness does not assert on
them.** ``_redact_obj`` rebuilds ``{k: _redact_obj(v) …}`` — the key is passed through
untouched, and the docstring scopes the promise to "each string **value**". So a frame
keyed by an identifier is not masked. That is recorded as an observation on the PR that
added this harness rather than asserted here: asserting it would report a documented
scope boundary as a crash on every batch run. The traversal below walks values only, and
the distinction is the reason it is written by hand instead of over ``repr(out)``.

Both traversals are **iterative**. A recursive checker would hit CPython's own limit on
the very inputs this harness exists to generate, and the crash would be the harness's.
"""

import sys

import atheris

with atheris.instrument_imports():
    # In-block per fuzz/README.md: `re` backs the leak matcher, and `hosted` pulls in
    # `redact` — the regexes doing the actual work — so both are instrumented here.
    import re

    from clauster import hosted

#: Mirrors ``redact._ID_RE`` exactly (``env_``/``session_``/``cse_`` + 6 or more chars).
#: Restated, not imported — see the module docstring.
_LEAK = re.compile(r"\b(env|session|cse)_[A-Za-z0-9]{6,}\b")

#: How far past ``_REDACT_MAX_DEPTH`` the deliberate deep chain may reach. Enough to be
#: unambiguously over the guard without making one input dominate the time budget.
_OVERSHOOT = 40

#: Bound on a single generated container's width, so one input cannot blow up quadratically.
_MAX_WIDTH = 6


def _text(fdp: atheris.FuzzedDataProvider) -> str:
    """A string leaf, decoded from raw bytes so dictionary tokens survive verbatim.

    ``ConsumeBytes`` + ``decode`` rather than ``ConsumeUnicodeNoSurrogates`` for the
    fuzz/README.md passthrough reason: the unicode consumer transforms its input, so the
    ``session_``/``sk-ant-`` literals in this harness's dictionary would rarely arrive at
    the redactor intact and the leak oracle would sit on the nothing-to-mask path.
    """
    return fdp.ConsumeBytes(fdp.ConsumeIntInRange(0, 48)).decode("utf-8", "replace")


def _leaf(fdp: atheris.FuzzedDataProvider) -> object:
    """A JSON scalar — the leaf types a parsed stream-json frame can hold."""
    pick = fdp.ConsumeIntInRange(0, 4)
    if pick == 0:
        return _text(fdp)
    if pick == 1:
        return fdp.ConsumeIntInRange(-1024, 1024)
    if pick == 2:
        return fdp.ConsumeBool()
    return None if pick == 3 else fdp.ConsumeFloat()


def _build(fdp: atheris.FuzzedDataProvider, budget: int) -> object:
    """Build a JSON-shaped frame, iteratively, with at most ``budget`` levels of nesting.

    Iterative so the *generator* cannot recurse past CPython's limit while producing the
    over-deep inputs this harness is aimed at. Containers are filled breadth-first from a
    worklist; each placeholder is replaced in its parent once its own children are built.
    """
    root: list[object] = [None]
    # (container, key-or-index, remaining depth budget)
    work: list[tuple] = [(root, 0, budget)]
    while work:
        parent, slot, left = work.pop()
        pick = fdp.ConsumeIntInRange(0, 5)
        if left <= 0 or pick <= 2 or fdp.remaining_bytes() < 8:
            parent[slot] = _leaf(fdp)
            continue
        width = fdp.ConsumeIntInRange(1, _MAX_WIDTH)
        if pick == 3:
            node: list[object] = [None] * width
            parent[slot] = node
            work.extend((node, i, left - 1) for i in range(width))
        else:
            keys = [_text(fdp) for _ in range(width)]
            mapping: dict = dict.fromkeys(keys)
            parent[slot] = mapping
            work.extend((mapping, k, left - 1) for k in mapping)
    return root[0]


def _values(obj: object):
    """Yield every node of ``obj``, walking dict VALUES and list items (never keys)."""
    stack = [obj]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _depth(obj: object) -> int:
    """The maximum container nesting depth of ``obj`` (a scalar is 0), computed iteratively."""
    deepest = 0
    stack = [(obj, 0)]
    while stack:
        node, level = stack.pop()
        if isinstance(node, dict | list):
            deepest = max(deepest, level + 1)
            children = node.values() if isinstance(node, dict) else node
            stack.extend((child, level + 1) for child in children)
    return deepest


def _shape_matches(src: object, dst: object, level: int) -> bool:
    """Whether ``dst`` preserves ``src``'s container shape down to the depth cap.

    At or past the cap the target is contracted to substitute its too-deep marker, so the
    comparison stops there rather than demanding a structure the contract forbids.
    """
    stack = [(src, dst, level)]
    while stack:
        a, b, depth = stack.pop()
        if depth >= hosted._REDACT_MAX_DEPTH:
            continue
        if isinstance(a, dict):
            if not isinstance(b, dict) or a.keys() != b.keys():
                return False
            stack.extend((a[k], b[k], depth + 1) for k in a)
        elif isinstance(a, list):
            if not isinstance(b, list) or len(a) != len(b):
                return False
            stack.extend((a[i], b[i], depth + 1) for i in range(len(a)))
    return True


def check(frame: object) -> None:
    """Assert every property above for one parsed frame.

    Split out of :func:`TestOneInput` so ``tests/test_fuzz_harness_smoke.py`` can drive
    the oracle from the suite: fuzz/README.md asks for proof that an assertion *can*
    fire, and an oracle that has never fired is indistinguishable from no oracle at all.
    """
    out = hosted._redact_obj(frame)

    for node in _values(out):
        if isinstance(node, str):
            assert not _LEAK.search(node), f"redaction leak in a hosted frame: {node!r}"

    assert _depth(out) <= hosted._REDACT_MAX_DEPTH, f"depth cap breached: {_depth(out)}"
    assert _shape_matches(frame, out, 0), "redaction changed the frame's shape"


def TestOneInput(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    # Half the budget goes to ordinary frames and half to frames built deliberately past
    # the guard: left to chance, random structural bytes would essentially never stack
    # 100 containers, and the depth cap — the branch #1326 made reachable — would go
    # unfuzzed while the harness reported healthy coverage of everything else.
    deep = fdp.ConsumeBool()
    budget = (
        fdp.ConsumeIntInRange(hosted._REDACT_MAX_DEPTH, hosted._REDACT_MAX_DEPTH + _OVERSHOOT)
        if deep
        else fdp.ConsumeIntInRange(0, 12)
    )
    check(_build(fdp, budget))


def main() -> None:
    atheris.Setup(sys.argv, TestOneInput)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
