#!/usr/bin/env python3
"""Generate the config-reference tables in ``docs/configuration.md`` from the models.

Single source of truth: every field's description lives in ``Field(description=...)``
in ``src/clauster/config.py``. This script renders one markdown table per config
model and splices it between ``<!-- BEGIN GEN: <key> -->`` / ``<!-- END GEN: <key> -->``
markers in the docs page, leaving all hand-written prose/admonitions/examples intact.

Usage::

    python scripts/gen_config_reference.py            # rewrite docs/configuration.md
    python scripts/gen_config_reference.py --check     # exit 1 if the page is stale

A pytest test runs ``--check`` so CI fails when a config field is added or changed
without regenerating the page (the drift this exists to prevent).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Literal, Union, get_args, get_origin

from pydantic import BaseModel

from clauster.config import (
    ApiConfig,
    AuthConfig,
    ClaudeConfig,
    ClausterConfig,
    ClaustrumConfig,
    CloneConfig,
    ConfigWriteConfig,
    DbConfig,
    InstanceDefaults,
    LoginShepherdConfig,
    LogsConfig,
    MetricsConfig,
    NotificationsConfig,
    ObservabilityConfig,
    ProjectConfig,
    ReaperConfig,
    ReverseProxyConfig,
    TlsConfig,
    UiConfig,
    UsageConfig,
    WebhooksConfig,
)
from clauster.config_editor import EDITABLE_FIELDS

DOCS_PAGE = Path(__file__).resolve().parent.parent / "docs" / "configuration.md"

# (marker key, model). The marker key matches `<!-- BEGIN GEN: <key> -->` in the page;
# headings + prose around each block stay hand-written.
SECTIONS: list[tuple[str, type[BaseModel]]] = [
    ("clauster", ClausterConfig),
    ("claude", ClaudeConfig),
    ("instance_defaults", InstanceDefaults),
    ("projects", ProjectConfig),
    ("auth", AuthConfig),
    ("db", DbConfig),
    ("reverse_proxy", ReverseProxyConfig),
    ("api", ApiConfig),
    ("ui", UiConfig),
    ("logs", LogsConfig),
    ("clone", CloneConfig),
    ("reaper", ReaperConfig),
    ("config_write", ConfigWriteConfig),
    ("login_shepherd", LoginShepherdConfig),
    ("usage", UsageConfig),
    ("metrics", MetricsConfig),
    ("observability", ObservabilityConfig),
    ("notifications", NotificationsConfig),
    ("webhooks", WebhooksConfig),
    ("claustrum", ClaustrumConfig),
    ("tls", TlsConfig),
]


def _is_nested(annotation: object) -> bool:
    """Whether a field is a nested model / map (rendered as its own section, not a row)."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return True
    return get_origin(annotation) is dict


def render_type(annotation: object) -> str:
    r"""Render a field annotation as a friendly, table-safe type string (``\|`` escaped)."""
    origin = get_origin(annotation)
    if origin is Literal:
        return " \\| ".join(f"`{a}`" for a in get_args(annotation))
    if origin in (Union, types.UnionType):
        args = get_args(annotation)
        rendered = " \\| ".join(render_type(a) for a in args if a is not type(None))
        if type(None) in args:
            rendered += " \\| null"
        return rendered
    if origin is list:
        inner = get_args(annotation)
        return f"list[{render_type(inner[0])}]" if inner else "list"
    simple = {bool: "bool", int: "int", float: "float", str: "str", Path: "path"}
    if annotation in simple:
        return simple[annotation]
    return getattr(annotation, "__name__", str(annotation))


def _render_value(val: object) -> str:
    """Render a default value as a code-formatted markdown cell."""
    if isinstance(val, BaseModel):
        return "*(see below)*"
    if val is None:
        return "`null`"
    if isinstance(val, bool):
        return f"`{'true' if val else 'false'}`"
    if isinstance(val, str):
        return '`""`' if val == "" else f"`{val}`"
    if isinstance(val, Path):
        # as_posix() so the rendered default is platform-independent — str(WindowsPath)
        # would emit backslashes (`~\.clauster`) and make the doc read "stale" on Windows.
        return f"`{val.as_posix()}`"
    if isinstance(val, list):
        if not val:
            return "`[]`"
        return "`[" + ", ".join(f'"{x}"' for x in val) + "]`"
    if isinstance(val, dict):
        return "`{}`"
    return f"`{val}`"


def render_default(field: object) -> str:
    """Render a field's default (``*(required)*`` when it has none)."""
    if field.is_required():  # type: ignore[attr-defined]
        return "*(required)*"
    if field.default_factory is not None:  # type: ignore[attr-defined]
        return _render_value(field.default_factory())  # type: ignore[attr-defined]
    return _render_value(field.default)  # type: ignore[attr-defined]


def render_table(model: type[BaseModel]) -> str:
    """Render the scalar fields of one model as a markdown table (nested models skipped)."""
    rows = ["| Key | Type | Default | Description |", "| --- | --- | --- | --- |"]
    for name, field in model.model_fields.items():
        if _is_nested(field.annotation):
            continue
        desc = (field.description or "").strip()
        cells = f"`{name}` | {render_type(field.annotation)} | {render_default(field)} | {desc}"
        rows.append(f"| {cells} |")
    return "\n".join(rows)


def render_editable_table() -> str:
    """Render the Tier-A config-editor allowlist, grouped by section, from ``EDITABLE_FIELDS``.

    The single source of truth is ``config_editor.EDITABLE_FIELDS`` (the same tuple the
    ``GET /api/config`` allowlist and the coverage-guard test read); grouping by the dotted
    path's section, in first-appearance order, keeps this table honest without a second
    hand-maintained copy.
    """
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for path in EDITABLE_FIELDS:
        section, _, leaf = path.rpartition(".")
        key = section or "(top-level)"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(leaf)
    rows = ["| Section | Editable fields |", "| --- | --- |"]
    for key in order:
        fields = ", ".join(f"`{leaf}`" for leaf in groups[key])
        rows.append(f"| `{key}` | {fields} |")
    return "\n".join(rows)


def _splice(text: str, key: str, content: str) -> str:
    """Replace the body between a block's BEGIN/END GEN markers with ``content``."""
    begin, end = f"<!-- BEGIN GEN: {key} -->", f"<!-- END GEN: {key} -->"
    if begin not in text or end not in text:
        raise SystemExit(f"missing markers for section {key!r} in {DOCS_PAGE}")
    head, rest = text.split(begin, 1)
    _, tail = rest.split(end, 1)
    return f"{head}{begin}\n{content}\n{end}{tail}"


def apply_blocks(text: str) -> str:
    """Splice every generated table (per-model, plus the editable-field allowlist)."""
    for key, model in SECTIONS:
        text = _splice(text, key, render_table(model))
    text = _splice(text, "editable_fields", render_editable_table())
    return text


def main(argv: list[str]) -> int:
    """Rewrite the docs page, or with ``--check`` report whether it is stale."""
    current = DOCS_PAGE.read_text(encoding="utf-8")
    updated = apply_blocks(current)
    if "--check" in argv:
        if current != updated:
            print(f"{DOCS_PAGE} is stale — run: python scripts/gen_config_reference.py")
            return 1
        print(f"{DOCS_PAGE} is up to date.")
        return 0
    if current != updated:
        DOCS_PAGE.write_text(updated, encoding="utf-8")
        print(f"updated {DOCS_PAGE}")
    else:
        print(f"{DOCS_PAGE} already up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
