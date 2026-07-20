#!/usr/bin/env python3
"""Generate the config-reference tables in the docs from the models.

Single source of truth: every field's description lives in ``Field(description=...)``
in ``src/clauster/config.py``. This script renders one markdown table per config
model and splices it between ``<!-- BEGIN GEN: <key> -->`` / ``<!-- END GEN: <key> -->``
markers in the docs pages, leaving all hand-written prose/admonitions/examples intact.
The per-model tables live in ``docs/reference/config.md``; the config-editor
allowlist tables live in ``docs/guides/config-editor.md``.

Usage::

    python scripts/gen_config_reference.py            # rewrite the stale pages
    python scripts/gen_config_reference.py --check     # exit 1 if a page is stale

A pytest test runs ``--check`` so CI fails when a config field is added or changed
without regenerating the pages (the drift this exists to prevent).
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
from clauster.config_editor import EDITABLE_FIELDS, TIER_B_FIELDS

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"
REFERENCE_PAGE = DOCS_DIR / "reference" / "config.md"
EDITOR_PAGE = DOCS_DIR / "guides" / "config-editor.md"

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


def render_tier_b_table() -> str:
    """Render the Tier-B "Advanced" allowlist, grouped by section, from ``TIER_B_FIELDS``.

    Same single-source pattern as :func:`render_editable_table`: the tuple in
    ``config_editor.TIER_B_FIELDS`` (which ``GET/PUT /api/config/advanced`` and the
    coverage-guard test read) drives the table, so it can never drift from the code.
    """
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for path in TIER_B_FIELDS:
        section, _, leaf = path.rpartition(".")
        key = section or "(top-level)"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(leaf)
    rows = ["| Section | Advanced fields |", "| --- | --- |"]
    for key in order:
        fields = ", ".join(f"`{leaf}`" for leaf in groups[key])
        rows.append(f"| `{key}` | {fields} |")
    return "\n".join(rows)


def _splice(text: str, key: str, content: str, page: Path) -> str:
    """Replace the body between a block's BEGIN/END GEN markers with ``content``."""
    begin, end = f"<!-- BEGIN GEN: {key} -->", f"<!-- END GEN: {key} -->"
    if begin not in text or end not in text:
        raise SystemExit(f"missing markers for section {key!r} in {page}")
    head, rest = text.split(begin, 1)
    _, tail = rest.split(end, 1)
    return f"{head}{begin}\n{content}\n{end}{tail}"


def apply_reference_blocks(text: str) -> str:
    """Splice every per-model table into the reference page's text."""
    for key, model in SECTIONS:
        text = _splice(text, key, render_table(model), REFERENCE_PAGE)
    return text


def apply_editor_blocks(text: str) -> str:
    """Splice the two config-editor allowlist tables into the editor guide's text."""
    text = _splice(text, "editable_fields", render_editable_table(), EDITOR_PAGE)
    return _splice(text, "tier_b_fields", render_tier_b_table(), EDITOR_PAGE)


def main(argv: list[str]) -> int:
    """Rewrite the docs pages, or with ``--check`` report whether any is stale."""
    stale = False
    pages = ((REFERENCE_PAGE, apply_reference_blocks), (EDITOR_PAGE, apply_editor_blocks))
    for page, apply in pages:
        current = page.read_text(encoding="utf-8")
        updated = apply(current)
        if current == updated:
            print(f"{page} is up to date.")
            continue
        if "--check" in argv:
            print(f"{page} is stale — run: python scripts/gen_config_reference.py")
            stale = True
        else:
            page.write_text(updated, encoding="utf-8")
            print(f"updated {page}")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
