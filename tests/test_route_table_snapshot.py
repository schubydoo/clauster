"""Golden route-table tests guarding the app.py -> routes/ refactor (#1156).

Moving a route from ``create_app`` into an ``APIRouter`` module must not change
the URL it answers on. Every auth exemption (`_V1_PUBLIC_ROUTES`,
`_UI_ONLY_ROUTES`, `_is_public`) keys on the literal path plus method, and
`_UI_ONLY_ROUTES` fails OPEN if a path stops matching, so a silent path drift is
a security regression, not a cosmetic one.

``test_route_table_matches_snapshot`` asserts the full route table -- HTTP routes
plus their `v1` mirror aliases, the WebSocket routes, and the `/static` mount --
matches the committed snapshot as a multiset, so an added, removed, or duplicated
route trips it. Reordering benign (non-overlapping) routes does not, which is
what a handler move does; the per-route functional tests in ``test_app_routes.py``
guard the ordering of the few overlapping literal-vs-parameter paths. Each row's
methods are compared sorted, so a multi-method route matches regardless of the
order stored in the JSON.

``test_ui_only_routes_all_resolve`` closes the `_UI_ONLY_ROUTES` fail-open: unlike
`_mirror_v1_routes`, nothing at runtime asserts each kill-switch entry still names
a live route, and ``test_app_ui_kill_switch.py`` passes vacuously (404) once a
route is renamed. This test fails loudly instead.

The snapshot is captured with ``api.openapi_enabled`` off (the default), so it
omits ``/docs`` and ``/openapi.json``. Update ``route_table_snapshot.json`` only
for a DELIBERATE route change, never to make a refactor pass. To regenerate it::

    CLAUSTER_UPDATE_ROUTE_SNAPSHOT=1 uv run pytest -o addopts="" \\
        tests/test_route_table_snapshot.py
"""

import json
import os
from collections import Counter
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import Mount, WebSocketRoute

from clauster.app import _UI_ONLY_ROUTES, _iter_api_routes, create_app
from clauster.config import load_config

_SNAPSHOT = Path(__file__).parent / "route_table_snapshot.json"
_UPDATE = os.environ.get("CLAUSTER_UPDATE_ROUTE_SNAPSHOT") == "1"


def _route_rows(app) -> list[tuple[tuple[str, ...], str]]:
    rows: list[tuple[tuple[str, ...], str]] = []

    def _walk(routes) -> None:
        for route in routes:
            if isinstance(route, APIRoute):
                rows.append((tuple(sorted((route.methods or set()) - {"HEAD"})), route.path))
            elif isinstance(route, WebSocketRoute):
                rows.append((("WS",), route.path))
            elif isinstance(route, Mount):
                rows.append((("MOUNT",), route.path))
            elif getattr(route, "original_router", None) is not None:
                # FastAPI 0.141 includes a router lazily as an _IncludedRouter wrapper
                # (#1156); descend into its real routes so a moved route still counts.
                # No prefix guard here (unlike app._iter_api_routes): _mirror_v1_routes
                # runs that guard during create_app, so a prefixed include fails at build
                # before this walk ever sees the app.
                _walk(route.original_router.routes)
            else:
                # Fail loudly on an unrecognized route class (e.g. a plain starlette
                # Route from app.add_route) rather than dropping it from the guard.
                rows.append((("UNKNOWN:" + type(route).__name__,), getattr(route, "path", "?")))

    _walk(app.router.routes)
    return rows


def test_route_table_matches_snapshot(write_config):
    actual_rows = _route_rows(create_app(load_config(write_config())))
    if _UPDATE:  # pragma: no cover - dev-only snapshot regeneration
        payload = sorted([list(methods), path] for methods, path in actual_rows)
        _SNAPSHOT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    expected = Counter(
        (tuple(sorted(methods)), path)
        for methods, path in json.loads(_SNAPSHOT.read_text("utf-8"))
    )
    actual = Counter(actual_rows)
    added = sorted((actual - expected).elements())
    removed = sorted((expected - actual).elements())
    assert actual == expected, (
        f"route table drifted from the snapshot.\nADDED/DUPED: {added}\nREMOVED: {removed}"
    )


def test_ui_only_routes_all_resolve(write_config):
    app = create_app(load_config(write_config()))
    registered = {
        (method, route.path)
        for route in _iter_api_routes(app.router.routes)
        for method in (route.methods or set())
    }
    missing = sorted(entry for entry in _UI_ONLY_ROUTES if entry not in registered)
    assert not missing, (
        f"_UI_ONLY_ROUTES entries name no live route (kill switch fails OPEN): {missing}"
    )
