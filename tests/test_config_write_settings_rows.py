"""Settings tab Rows mode: the typed-row hints in the read payload (TASK-271).

Covers the key catalog that picks a row editor (its shape, and that it holds no key that
no clauster scope reads), the per-scope ``here`` flag, the payload that carries the catalog
and the owned-key tabs, and the round trip of every JSON type a row can hold: a save sends
each value back with the JSON type it was loaded with, and a resent mask keeps the stored
credential.

Every test that touches ``~/.claude/settings.json`` runs under the autouse HOME-isolation
fixture and writes only into the isolated tmp home.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clauster import config_write as cw
from clauster import config_write_settings as cws
from clauster.app import create_app
from clauster.config import load_config

FAKE_CLAUDE = Path(__file__).resolve().parent / "fixtures" / "fake_claude" / "claude"
_ON = "config_write:\n  enabled: true\n  allow_user_scope: true\n"
_URL = "/api/config-write/settings"


def _client(write_config, tmp_path: Path) -> TestClient:
    cfg = write_config(f"claude:\n  binary: {FAKE_CLAUDE}\nstate_dir: {tmp_path}/.s\n{_ON}")
    return TestClient(create_app(load_config(cfg)))


# --- the catalog ---------------------------------------------------------------------


def test_catalog_entries_are_well_formed() -> None:
    for key, kind in cws.ROW_KEY_TYPES.items():
        assert "." not in key, key  # top-level keys only
        if isinstance(kind, tuple):
            assert kind, key
            assert all(isinstance(v, str) and v for v in kind), key
            assert len(set(kind)) == len(kind), key
        else:
            assert kind in {"boolean", "number", "string"}, key


def test_catalog_holds_no_key_that_no_clauster_scope_reads() -> None:
    # A managed-only, ~/.claude.json-only or Desktop-only key is refused at every scope the
    # tab writes, so offering it as a row would only lead to a refusal.
    for key in cws.ROW_KEY_TYPES:
        assert cws.SCOPE_LIMITED_KEYS.get(key) not in {"managed", "global"}, key
        assert key not in cws.DESKTOP_ONLY_KEYS, key


def test_catalog_holds_no_carved_or_env_key() -> None:
    assert not cws.OWNED_KEYS & cws.ROW_KEY_TYPES.keys()
    assert cws.ENV_KEY not in cws.ROW_KEY_TYPES


def test_catalog_payload_shape() -> None:
    catalog = cws.row_key_catalog("project")
    assert catalog.keys() == cws.ROW_KEY_TYPES.keys()
    assert catalog["verbose"] == {"type": "boolean", "here": True}
    assert catalog["cleanupPeriodDays"] == {"type": "number", "here": True}
    assert catalog["model"] == {"type": "string", "here": True}
    assert catalog["tui"] == {"type": "enum", "values": ["default", "fullscreen"], "here": True}


@pytest.mark.parametrize(
    ("key", "here"),
    [
        ("skipAutoPermissionPrompt", {"user": True, "project": False, "local": False}),
        ("useAutoModeDuringPlan", {"user": True, "project": False, "local": True}),
        ("verbose", {"user": True, "project": True, "local": True}),
        # Value-level only: false is read everywhere, so the key is offered everywhere and
        # a true value at project scope is refused on save, not hidden.
        ("bashEditDiffEnabled", {"user": True, "project": True, "local": True}),
    ],
)
def test_catalog_here_follows_the_scope_rule(key: str, here: dict[str, bool]) -> None:
    for scope, expected in here.items():
        assert cws.row_key_catalog(scope)[key]["here"] is expected, scope  # type: ignore[arg-type]


# --- the read payload ----------------------------------------------------------------


@pytest.mark.parametrize("scope", ["user", "project", "local"])
def test_read_payload_carries_row_hints(write_config, tmp_path, projects_root, scope) -> None:
    query = "scope=user" if scope == "user" else f"scope={scope}&project=alpha"
    with _client(write_config, tmp_path) as c:
        resp = c.get(f"{_URL}?{query}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["key_types"] == cws.row_key_catalog(scope)  # type: ignore[arg-type]
    assert body["owned_keys"] == cws.OWNED_KEY_TABS


# --- type fidelity -------------------------------------------------------------------

_TYPED: dict[str, Any] = {
    "verbose": True,
    "includeCoAuthoredBy": False,
    "cleanupPeriodDays": 30,
    "feedbackSurveyRate": 0.25,
    "theme": "dark",
    "language": "1",  # a string that looks like a number stays a string
    "someFutureKey": "x",
    "attribution": False,
    "teammateDefaultModelish": None,
    "extraKnownMarketplaces": {"m": {"source": {"source": "github", "repo": "o/r"}}},
    "companyAnnouncements": ["a", "b"],
    "env": {"A": "1"},
}


def _types(data: dict[str, Any]) -> dict[str, str]:
    return {k: type(v).__name__ for k, v in data.items()}


def test_route_round_trip_keeps_every_json_type(write_config, tmp_path, projects_root) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps(_TYPED), encoding="utf-8")
    with _client(write_config, tmp_path) as c:
        r0 = c.get(f"{_URL}?project=alpha").json()
        view = r0["settings"]
        assert _types({k: v for k, v in view.items() if k != "env"}) == _types(
            {k: v for k, v in _TYPED.items() if k != "env"}
        )
        # Rows mode sends the loaded view back with one toggle flipped; every other value
        # (the masked env included) must land with the type it had.
        view["verbose"] = False
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "settings": view,
                "hash": r0["hash"],
            },
        )
        assert resp.status_code == 200, resp.text
    stored = json.loads(settings.read_text(encoding="utf-8"))
    assert stored == {**_TYPED, "verbose": False}
    assert _types(stored) == _types(_TYPED)
    assert stored["cleanupPeriodDays"] == 30 and type(stored["cleanupPeriodDays"]) is int


def test_route_masked_helper_row_keeps_stored_value(write_config, tmp_path, projects_root) -> None:
    settings = projects_root / "alpha" / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"apiKeyHelper": "/opt/print-key --token s3cr3t", "verbose": True}),
        encoding="utf-8",
    )
    with _client(write_config, tmp_path) as c:
        r0 = c.get(f"{_URL}?project=alpha").json()
        assert r0["settings"]["apiKeyHelper"] == cw.REDACTION_SENTINEL
        assert "s3cr3t" not in json.dumps(r0)
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "settings": {"apiKeyHelper": cw.REDACTION_SENTINEL, "verbose": False},
                "hash": r0["hash"],
            },
        )
        assert resp.status_code == 200, resp.text
    stored = json.loads(settings.read_text(encoding="utf-8"))
    assert stored == {"apiKeyHelper": "/opt/print-key --token s3cr3t", "verbose": False}


def test_route_refused_row_key_returns_the_backend_refusal(
    write_config, tmp_path, projects_root
) -> None:
    # The tab shows this detail as it is; it never makes its own guess at the rule.
    with _client(write_config, tmp_path) as c:
        resp = c.put(
            _URL,
            json={
                "scope": "project",
                "project": "alpha",
                "confirm": "alpha",
                "settings": {"skipAutoPermissionPrompt": True},
            },
        )
    assert resp.status_code == 422
    assert resp.json()["detail"] == cws._scope_refusal("skipAutoPermissionPrompt", "project")
    assert not (projects_root / "alpha" / ".claude" / "settings.json").exists()
