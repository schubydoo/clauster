"""Guards for the FastAPI route-field memoization that keeps the suite fast (#1128).

The cache is test infrastructure, so nothing in ``src/`` exercises it. These tests are
what stands between a FastAPI/pydantic upgrade and a silent regression — either losing
the speedup (a binding moved) or, far worse, sharing a ``ModelField`` that is no longer
safe to share.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import _route_field_cache as rfc


def _snapshot_stats() -> dict[str, int]:
    return dict(rfc.STATS)


@pytest.mark.skipif(
    bool(os.environ.get(rfc.DISABLE_ENV)),
    reason=f"cache deliberately disabled via {rfc.DISABLE_ENV}",
)
def test_cache_is_installed_on_every_binding():
    # FastAPI imports create_model_field by value into the two hot call sites; if an
    # upgrade moves or drops a binding, the suite silently pays full price again.
    # Skipped under the disable env var, so the documented triage step ("turn it off and
    # re-run") doesn't hand back a spurious failure.
    assert set(rfc.install()) == {m.__name__ for m in rfc.BINDING_MODULES}
    for module in rfc.BINDING_MODULES:
        assert module.create_model_field is rfc.cached_create_model_field


def test_wrapper_signature_matches_fastapi():
    # A drop-in replacement must accept every call shape FastAPI uses, positional
    # included. A parameter added, renamed or reordered upstream shows up here.
    original = inspect.signature(rfc._ORIGINAL)
    wrapper = inspect.signature(rfc.cached_create_model_field)
    # Kind as well as name: the wrapper forwards positionally, so a parameter turned
    # keyword-only upstream would break the call without changing the name list. Default
    # too, so an upstream change to `mode`'s or `alias`'s default doesn't slip through.
    # (`_NO_DEFAULT` is derived FROM this signature, so it is not what is checked here.)
    assert [(p.name, p.kind, p.default) for p in original.parameters.values()] == [
        (p.name, p.kind, p.default) for p in wrapper.parameters.values()
    ]


def test_identical_requests_hit_the_cache():
    before = _snapshot_stats()
    first = rfc.cached_create_model_field(name="cache_probe", type_=int, mode="serialization")
    second = rfc.cached_create_model_field(name="cache_probe", type_=int, mode="serialization")
    assert first is second
    assert rfc.STATS["hits"] == before["hits"] + 1
    assert rfc.STATS["misses"] == before["misses"] + 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "probe_other_name", "type_": int, "mode": "serialization"},
        {"name": "probe_distinct", "type_": str, "mode": "serialization"},
        {"name": "probe_distinct", "type_": int, "mode": "validation"},
    ],
)
def test_differing_inputs_do_not_share_a_field(kwargs):
    base = rfc.cached_create_model_field(name="probe_distinct", type_=int, mode="serialization")
    other = rfc.cached_create_model_field(**kwargs)
    assert other is not base
    # Paired positive: "is not" would also hold with the cache off or broken, which would
    # make the negative above vacuous. The repeat proves the cache is genuinely serving.
    again = rfc.cached_create_model_field(name="probe_distinct", type_=int, mode="serialization")
    assert again is base


def test_field_infos_differing_only_in_constraints_do_not_share_a_field():
    # The hazard the key fingerprint exists for: `fastapi.params.*` override __repr__ to
    # class-name + default, so a repr-based key would hand the constrained route the
    # unconstrained field and silently drop its input guard.
    from fastapi import params

    loose = params.Query(default=50, annotation=int)
    tight = params.Query(default=50, annotation=int, le=10)
    assert repr(loose) == repr(tight)  # the trap: identical reprs, different validation
    a = rfc.cached_create_model_field(name="limit", type_=int, default=50, field_info=loose)
    b = rfc.cached_create_model_field(name="limit", type_=int, default=50, field_info=tight)
    assert a is not b
    assert not a.validate(999)[1], "the unconstrained field must still accept 999"
    assert b.validate(999)[1], "the le=10 constraint must survive the cache"


def test_field_infos_differing_only_in_source_do_not_share_a_field():
    # `in_` is the other attribute pydantic's __repr_args__ omits: the same name and type
    # read from the query string vs from a header. Deliberately built from bare `Param`s
    # with `in_` assigned afterwards — the way FastAPI does it (dependencies/utils.py) —
    # because `Query(...)` vs `Header(...)` would differ by class name too, and then the
    # class name, not `in_`, would be doing the work and this guard would be decorative.
    from fastapi import params

    query = params.Param(default="x", annotation=str)
    query.in_ = params.ParamTypes.query
    header = params.Param(default="x", annotation=str)
    header.in_ = params.ParamTypes.header
    assert repr(query) == repr(header)  # the trap: identical reprs, different source
    assert type(query).__qualname__ == type(header).__qualname__
    assert rfc.cached_create_model_field(
        name="token", type_=str, default="x", field_info=query
    ) is not rfc.cached_create_model_field(name="token", type_=str, default="x", field_info=header)


def test_field_infos_differing_only_in_default_factory_do_not_share_a_field():
    # pydantic renders a factory as its display name (`<lambda>`), so a repr-only key would
    # hand the second route the first route's default. Carried by identity instead.
    from fastapi import params

    one = params.Query(default_factory=lambda: 1, annotation=int)
    two = params.Query(default_factory=lambda: 2, annotation=int)
    first = rfc.cached_create_model_field(name="page", type_=int, field_info=one)
    second = rfc.cached_create_model_field(name="page", type_=int, field_info=two)
    assert first is not second
    assert first.get_default() == 1
    assert second.get_default() == 2


def test_unhashable_type_bypasses_the_cache_instead_of_failing(monkeypatch):
    # The key is a tuple containing `type_`; an unhashable annotation must degrade to
    # "build it every time", never to a TypeError that takes the whole suite down.
    class Unhashable:
        __hash__ = None  # type: ignore[assignment]

    calls = []
    monkeypatch.setattr(rfc, "_ORIGINAL", lambda *args: calls.append(args) or "built")
    before = _snapshot_stats()
    assert rfc.cached_create_model_field(name="probe_unhashable", type_=Unhashable()) == "built"
    assert len(calls) == 1
    assert rfc.STATS["uncacheable"] == before["uncacheable"] + 1
    assert rfc.STATS["misses"] == before["misses"]


def test_a_failed_build_is_not_memoized():
    # Caching a failure would turn one bad response model into a permanently poisoned
    # key for the rest of the worker's session. Same key both times, so a memoized
    # failure would show up as a hit.
    bad_model = object()
    before = _snapshot_stats()
    for _ in range(2):
        with pytest.raises(Exception):  # noqa: B017 — the type is upstream's to choose
            rfc.cached_create_model_field(name="probe_bad", type_=bad_model, mode="serialization")
    assert rfc.STATS["hits"] == before["hits"]
    assert rfc.STATS["misses"] == before["misses"] + 2


def test_cache_stops_growing_at_the_cap(monkeypatch):
    # Insurance against an upstream change that keys a field on a freshly built type:
    # growth would become per-app rather than per-route, so the cache must stop growing.
    monkeypatch.setattr(rfc, "MAX_ENTRIES", len(rfc._CACHE))
    before = len(rfc._CACHE)
    field = rfc.cached_create_model_field(name="probe_capped", type_=int, mode="serialization")
    assert field is not None
    assert len(rfc._CACHE) == before
    # ...and an uncached key stays uncached, i.e. it is a real miss the next time round.
    misses = rfc.STATS["misses"]
    rfc.cached_create_model_field(name="probe_capped", type_=int, mode="serialization")
    assert rfc.STATS["misses"] == misses + 1


def test_disable_env_skips_installation(monkeypatch):
    # The escape hatch a triage step relies on: with the env var set, conftest's
    # install() must be a no-op so FastAPI builds every field for real.
    monkeypatch.setenv(rfc.DISABLE_ENV, "1")
    assert rfc.install() == ()


def test_two_apps_reject_identically_through_the_cached_fields(write_config, tmp_path):
    # The end-to-end guarantee. Both rejections must come from *inside pydantic*, before
    # the handler body runs — a handler-level `raise HTTPException(422)` would pass no
    # matter what the cache returned, which is the vacuous shape AGENTS.md warns about:
    #   * `json=[]` violates the `body: dict` annotation on api_create_project.
    #   * `offset=abc` violates `offset: int = 0` on api_project_transcript_tail.
    # Asserting the error *type*, not just the 422: those same routes also 422 from the
    # handler, so a future signature change could silently return this to vacuous.
    from clauster.app import create_app
    from clauster.config import load_config

    def build():
        return create_app(load_config(write_config(f"state_dir: {tmp_path}/.s\n")))

    first, second = build(), build()
    assert first is not second
    for app in (first, second):
        with TestClient(app) as client:
            body = client.post("/api/projects", json=[])
            assert body.status_code == 422
            assert body.json()["detail"][0]["type"] == "dict_type"
            tail = client.get("/api/projects/p/transcripts/s/tail", params={"offset": "abc"})
            assert tail.status_code == 422
            assert tail.json()["detail"][0]["type"] == "int_parsing"
    # Distinct apps must keep distinct state even though their route fields are shared.
    assert first.state.runner is not second.state.runner
    assert first.state.config is not second.state.config


def _openapi_config(write_config, tmp_path) -> Path:
    return write_config(f"state_dir: {tmp_path}/.s\napi:\n  openapi_enabled: true\n")


@pytest.mark.skipif(
    bool(os.environ.get(rfc.DISABLE_ENV)),
    reason=f"nothing is shared under {rfc.DISABLE_ENV}, so this is trivially satisfied",
)
def test_openapi_document_is_stable_across_apps(write_config, tmp_path):
    # `ModelField.__hash__` is `id(self)`, and FastAPI keys its JSON-Schema definitions
    # dict on the field object — so sharing fields across apps collapses some of those
    # keys. Benign, but it is the one place the sharing is observable, so pin it: emitting
    # the first app's document must not perturb the shared field state the second app's
    # document is built from. A drift here would mean the schema depends on build order.
    # Detection boundary, so nobody reads more into it than it checks: this catches a
    # key-space COLLAPSE (e.g. `name` dropped from `_cache_key` as an "optimization");
    # fingerprint-level bugs are caught by the three `test_field_infos_differing_only_in_*`
    # guards above, not here.
    import json

    from clauster.app import create_app
    from clauster.config import load_config

    cfg = _openapi_config(write_config, tmp_path)
    first = create_app(load_config(cfg))
    first_doc = json.dumps(first.openapi(), sort_keys=True)
    second = create_app(load_config(cfg))
    assert json.dumps(second.openapi(), sort_keys=True) == first_doc


@pytest.mark.skipif(
    bool(os.environ.get(rfc.DISABLE_ENV)),
    reason=f"both arms would be uncached under {rfc.DISABLE_ENV}, making this vacuous",
)
def test_openapi_document_is_identical_cached_and_uncached(write_config, tmp_path, monkeypatch):
    # The claim the module docstring makes, as a test rather than as narrative: the shared
    # ModelField objects produce the very same /openapi.json the unshared ones do. The
    # second app is built with the real `create_model_field` restored on every binding, so
    # this is a genuine on-vs-off comparison rather than cached-vs-cached.
    import json

    from clauster.app import create_app
    from clauster.config import load_config

    cfg = _openapi_config(write_config, tmp_path)
    cached = json.dumps(create_app(load_config(cfg)).openapi(), sort_keys=True)
    for module in rfc.BINDING_MODULES:
        monkeypatch.setattr(module, "create_model_field", rfc._ORIGINAL)
    uncached = json.dumps(create_app(load_config(cfg)).openapi(), sort_keys=True)
    assert cached == uncached
