"""Memoize FastAPI's per-route pydantic field construction across the suite's app builds.

Every one of the ~100 ``@app.<verb>`` handlers in ``app.py`` is declared *inside*
``create_app``, so each call rebuilds all ~105 routes from scratch: for every route
FastAPI derives a ``Dependant`` from the handler signature and builds a pydantic
``TypeAdapter`` for each parameter and response field. ~1170 tests build an app, and
route registration is by far the largest single slice of the suite's CPU: measured on
Linux at ~200 CPU-seconds of a ~600 CPU-second run (`APIRoute.__init__`, coverage
tracing on) — roughly 80% of ``create_app`` (issue #1128).

**None of that work depends on the config.** The handlers are recreated closures, but
their *signatures* — the only thing FastAPI reads here — are identical every time,
because ``app.py`` uses ``from __future__ import annotations`` (annotations are
strings resolved against the module globals) and declares no ``Depends``. So the
field construction is a pure function of ``(field name, type, mode, field_info)``,
repeated ~20 000 times per xdist worker with ~114 distinct results.

This caches that one pure step. It is the alternative to #1128's suggested
session-scoped shared app, and was chosen over it deliberately: a shared app shares
``app.state`` — the runner, registries, auth state, the claustrum daemon handle — and
cross-test leakage through any of those is expensive to diagnose and easy to mistake
for flakiness (the issue says so itself). Nothing here is shared between tests: every
test still builds its own app, its own runner, and its own ``app.state``. The only
objects that outlive a test are ``ModelField`` instances, which are stateless
validator/serializer wrappers.

Why sharing a ``ModelField`` between two apps is safe:

* ``fastapi._compat.v2.ModelField`` is a dataclass of ``(field_info, name, mode,
  config)`` plus a ``TypeAdapter`` built once in ``__post_init__``. FastAPI never
  mutates any of it after construction (verified against the pinned version), and
  ``validate`` / ``serialize`` / ``get_default`` only read.
* It holds no reference to the endpoint, the app, the config, or the runner — those
  live on the ``Dependant``, which is **not** cached and is still built per app.
* The cache key pins everything ``create_model_field`` reads, and the key space is
  bounded by the app's route table rather than by the number of tests. ⚠️ ``name`` is
  **not** by itself a discriminator: it is the route's ``unique_id`` only for the three
  response-side call sites, while a request param is keyed on the bare Python parameter
  name and an embedded body on the constant ``"body"``. What actually separates two
  same-named params on different routes is the ``field_info`` fingerprint — which is
  why that fingerprint must not be ``repr(field_info)``. See
  :func:`_field_info_fingerprint`.

A failed build is never cached, so a test asserting that a bad response model raises
still gets the real exception.

One assumption worth stating rather than leaving to be rediscovered: ``ModelField`` defines
``__hash__`` as ``id(self)``, commented upstream as "each ModelField is unique for our
purposes, to allow making a dict from ModelField to its JSON Schema". Sharing does collapse
some of those dict keys — in one clauster app the ~79 param-field slots become 16 distinct
objects, 6 shared across routes. It is benign here, and checked rather than assumed:
``test_openapi_document_is_identical_cached_and_uncached`` asserts ``/openapi.json`` is
byte-identical with the cache on and off.

Set ``CLAUSTER_TEST_NO_ROUTE_FIELD_CACHE=1`` to turn this off — the first thing to try
when triaging a suspected route/validation oddity, to rule the cache in or out.
"""

from __future__ import annotations

import inspect
import os
from typing import Any

import fastapi.dependencies.utils
import fastapi.routing
import fastapi.utils
from pydantic.fields import FieldInfo

#: Env var that disables the cache (set to any non-empty value).
DISABLE_ENV = "CLAUSTER_TEST_NO_ROUTE_FIELD_CACHE"

#: Modules that hold a by-value binding of ``create_model_field``. FastAPI imports the
#: symbol (``from fastapi.utils import create_model_field``) rather than calling it
#: through the module, so patching ``fastapi.utils`` alone would leave the hot call
#: sites in ``routing`` and ``dependencies.utils`` on the original. ``install`` reports
#: which of these it actually patched; ``test_route_field_cache.py`` asserts the set is
#: still complete, so a FastAPI upgrade that moves a binding fails one named test
#: instead of silently losing the speedup.
BINDING_MODULES = (fastapi.utils, fastapi.routing, fastapi.dependencies.utils)

# Captured at import, so a test that monkeypatched `fastapi.utils.create_model_field`
# would be bypassed rather than wrapped. Nothing in the suite does (this module and its
# own test are the only mentions), and the alternative — resolving through the module on
# every call — would reinstate the per-call lookup this exists to remove.
_ORIGINAL = fastapi.utils.create_model_field
# FastAPI's own sentinel for "no default" — read off the real signature rather than
# imported, so this keeps matching if the sentinel moves.
_NO_DEFAULT = inspect.signature(_ORIGINAL).parameters["default"].default

_CACHE: dict[Any, Any] = {}

#: Hard ceiling on cache entries. The key space is bounded by the app's route table, not
#: by the number of tests — a full run settles at 114 entries per xdist worker, identical
#: in every worker. The cap only matters if a future FastAPI starts keying a field on a
#: freshly built type (``_get_body_field`` already builds a dynamic model, it just isn't
#: reached by clauster's routes): growth would then be per-app rather than per-route, and
#: this stops it becoming a memory leak. Past the cap the cache stops *growing*; it keeps
#: serving what it has, so behaviour is unchanged either way.
MAX_ENTRIES = 4096

#: Observability for the accompanying test (and for a quick sanity check when the
#: suite's timing changes unexpectedly).
STATS = {"hits": 0, "misses": 0, "uncacheable": 0}


#: ``FieldInfo`` attributes that ``fastapi.params.*`` adds and pydantic's ``__repr_args__``
#: does not cover. ``in_`` separates a query param from a same-named header, ``embed`` and
#: ``media_type`` change how a body is parsed, ``convert_underscores`` rewrites a header's
#: wire name, and ``deprecated`` / ``include_in_schema`` / ``openapi_examples`` / ``example``
#: change ``/openapi.json`` (which `test_app_public_api.py` asserts on).
_FASTAPI_FIELD_INFO_ATTRS = (
    "in_",
    "embed",
    "media_type",
    "convert_underscores",
    "deprecated",
    "include_in_schema",
    "openapi_examples",
    "example",
)


def _field_info_fingerprint(field_info: Any) -> tuple:
    """Fingerprint a ``FieldInfo`` by everything that changes how it behaves.

    ⚠️ NOT ``repr(field_info)``. Every parameter FastAPI builds is a
    ``fastapi.params.*`` subclass, and those override ``__repr__`` to
    ``f"{class name}({self.default})"`` (``fastapi/params.py``) — so annotation, alias and
    **every constraint** (``le``, ``ge``, ``min_length``, ``pattern``, …) drop out. Two
    same-named ``int`` params, one constrained and one not, would then share a key and the
    constrained route would silently lose its input guard while the suite stayed green.
    Going through ``FieldInfo.__repr_args__`` explicitly bypasses the subclass stub and
    gets pydantic's own account: annotation, default, alias, and all metadata.

    ``default_factory`` is carried by **identity**, not through that repr: pydantic renders
    it as a display name only (``<lambda>``), so two params whose factories differ would
    otherwise collide and the second route would silently take the first's default. The key
    holds a strong reference to the callable, so an id can't be recycled underneath it, and
    ``_cache_key``'s ``hash()`` guard already covers an unhashable one.
    """
    return (
        type(field_info).__qualname__,
        repr(list(FieldInfo.__repr_args__(field_info))),
        getattr(field_info, "default_factory", None),
        tuple(repr(getattr(field_info, attr, None)) for attr in _FASTAPI_FIELD_INFO_ATTRS),
    )


def _cache_key(
    name: str, type_: Any, default: Any, field_info: Any, alias: str | None, mode: str
) -> tuple:
    """Build a hashable key covering every input ``create_model_field`` reads.

    ``default`` and ``alias`` are only consulted when ``field_info`` is None (FastAPI
    builds a ``FieldInfo`` from them in that case, and ignores them otherwise), so the key
    carries one branch or the other rather than both.

    ``name`` alone is not a discriminator: it is the route's ``unique_id`` for the three
    response-side call sites, but for request params it is only the Python parameter name
    (``fastapi/dependencies/utils.py``) and for an embedded body it is the constant
    ``"body"``. The ``field_info`` fingerprint is therefore what keeps two same-named
    params on different routes apart — see :func:`_field_info_fingerprint`.

    Raises ``TypeError`` for an unhashable key; the caller then bypasses the cache.
    """
    fingerprint = (
        (repr(default), alias) if field_info is None else _field_info_fingerprint(field_info)
    )
    key = (name, type_, mode, fingerprint)
    hash(key)  # fail here, not on insert, so the caller can fall back cleanly
    return key


def cached_create_model_field(
    name: str,
    type_: Any,
    default: Any = _NO_DEFAULT,
    field_info: Any = None,
    alias: str | None = None,
    mode: str = "validation",
) -> Any:
    """Return a memoized ``ModelField``, building it on first request.

    Mirrors ``fastapi.utils.create_model_field``'s signature exactly (the accompanying
    test asserts that), so it is a drop-in for both positional and keyword call sites.
    """
    try:
        key = _cache_key(name, type_, default, field_info, alias, mode)
    except TypeError:
        # An unhashable type or default: no key, so no caching. Never a failure —
        # just the original cost for that field.
        STATS["uncacheable"] += 1
        return _ORIGINAL(name, type_, default, field_info, alias, mode)
    if key in _CACHE:
        STATS["hits"] += 1
        return _CACHE[key]
    STATS["misses"] += 1
    # Deliberately outside the cache write: a build that raises (an invalid response
    # model) must raise again for the next caller, not be remembered.
    field = _ORIGINAL(name, type_, default, field_info, alias, mode)
    if len(_CACHE) < MAX_ENTRIES:
        _CACHE[key] = field
    return field


def install() -> tuple[str, ...]:
    """Patch every by-value binding of ``create_model_field``; return the names patched.

    Returns an empty tuple when :data:`DISABLE_ENV` is set. Idempotent: a module already
    carrying the wrapper is left alone — so a second call can't nest wrappers — but is
    still reported, because the return value answers "which bindings the cache serves",
    not "which ones this call changed".
    """
    if os.environ.get(DISABLE_ENV):
        return ()
    patched = []
    for module in BINDING_MODULES:
        if getattr(module, "create_model_field", None) is _ORIGINAL:
            module.create_model_field = cached_create_model_field
        if getattr(module, "create_model_field", None) is cached_create_model_field:
            patched.append(module.__name__)
    return tuple(patched)
