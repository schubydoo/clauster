"""Form-field accessibility contract (#1044) — id/name + an accessible name on every control.

The dogfood bug bash surfaced two DevTools advisories on the dashboard: *"a form field
element should have an id or name attribute"* and *"no label associated with a form
field"*. Both are static properties of the templates, so they are guarded here rather
than in the opt-in browser suite — a new control that forgets them fails on the cheap
gate instead of waiting for ``scripts/e2e.sh`` (which still runs axe-core over the live
page in ``tests/e2e/test_a11y_e2e.py``).

The scanner walks the template source, not the rendered page, because most of these
controls live inside Alpine ``x-for`` rows that only exist client-side. It is
quote-aware (Alpine expressions such as ``x-show="a > b"`` contain ``>``), blanks out
comments first (a ``<select>`` named in a JS comment is not a control), and treats
Alpine's bound forms — ``:id``, ``:name``, ``:aria-label`` — as equivalent to the static
attributes. ``test_scanner_flags_a_known_offender`` proves the instrument can still
produce a positive, so a green sweep means "clean", not "broken scanner".
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

import clauster
from clauster.app import create_app
from clauster.config import load_config

_TEMPLATES = Path(clauster.__file__).resolve().parent / "templates"

_CONTROLS = {"input", "select", "textarea", "button"}
_FIELDS = {"input", "select", "textarea"}  # the id/name advisory applies to these only
_BOUND = ("", ":", "x-bind:")  # static, Alpine shorthand, Alpine longhand

_ATTR_RE = re.compile(
    r"""([@:A-Za-z_][A-Za-z0-9_.:@\[\]-]*)\s*(?:=\s*("([^"]*)"|'([^']*)'|([^\s>]+)))?""",
    re.S,
)


def _blank(match: re.Match[str]) -> str:
    """Replace a comment with spaces so byte offsets and line numbers survive."""
    return re.sub(r"[^\n]", " ", match.group(0))


def _strip_comments(text: str) -> str:
    text = re.sub(r"\{#.*?#\}", _blank, text, flags=re.S)  # Jinja
    text = re.sub(r"<!--.*?-->", _blank, text, flags=re.S)  # HTML
    text = re.sub(r"/\*.*?\*/", _blank, text, flags=re.S)  # CSS / JS block
    return re.sub(r"(?m)^[ \t]*//.*$", _blank, text)  # JS line


def _scan_tags(text: str):
    """Yield ``(pos, raw, name, is_close, is_self_close, end)`` for every tag."""
    i, n = 0, len(text)
    while i < n:
        lt = text.find("<", i)
        if lt < 0:
            return
        j = lt + 1
        is_close = text[j : j + 1] == "/"
        if is_close:
            j += 1
        match = re.match(r"[A-Za-z][A-Za-z0-9-]*", text[j:])
        if match is None:
            i = lt + 1
            continue
        k, quote = j + len(match.group(0)), None
        while k < n:
            char = text[k]
            if quote is not None:
                if char == quote:
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == ">":
                break
            k += 1
        if k >= n:
            return
        raw = text[lt : k + 1]
        yield lt, raw, match.group(0).lower(), is_close, raw.rstrip().endswith("/>"), k + 1
        i = k + 1


def _attrs(raw: str) -> dict[str, str]:
    body = re.sub(r"^\s*/?[A-Za-z][A-Za-z0-9-]*", "", raw[1:-1].rstrip("/"), count=1)
    return {
        m.group(1).lower(): (m.group(3) or m.group(4) or m.group(5) or "")
        for m in _ATTR_RE.finditer(body)
    }


def _has(attrs: dict[str, str], name: str) -> bool:
    return any(prefix + name in attrs for prefix in _BOUND)


def _values(attrs: dict[str, str], name: str) -> set[str]:
    """Normalised values of ``name`` in every binding form, so ``for``/``id`` compare equal."""
    return {
        re.sub(r"\s+", "", attrs[prefix + name]).replace('"', "'")
        for prefix in _BOUND
        if prefix + name in attrs
    }


def _names_itself(attrs: dict[str, str], text: str, end: int) -> bool:
    """True when a ``<button>`` supplies its own name.

    Three sources all render text at runtime: literal/Jinja content, an ``x-text`` on
    the button itself, and an ``x-text`` on a descendant (the icon+label idiom).
    """
    if _has(attrs, "x-text") or _has(attrs, "x-html"):
        return True
    close = text.find("</button", end)
    inner = text[end:close] if close > 0 else text[end : end + 600]
    if re.search(r"\bx-(?:text|html)\s*=", inner):
        return True
    return bool(re.sub(r"<[^>]*>", " ", inner).strip())


def _controls(source: str, origin: str) -> tuple[list[dict], set[str]]:
    """Return every visible form control in ``source`` plus the label targets it declares."""
    text = _strip_comments(source)
    rows: list[dict] = []
    label_targets: set[str] = set()
    depth = 0
    for pos, raw, tag, is_close, self_close, end in _scan_tags(text):
        if tag == "label":
            if is_close:
                depth = max(0, depth - 1)
            else:
                depth += 0 if self_close else 1
                label_targets |= _values(_attrs(raw), "for")
            continue
        if is_close or tag not in _CONTROLS:
            continue
        attrs = _attrs(raw)
        if tag == "input" and attrs.get("type", "").lower() == "hidden":
            continue  # not focusable, not exposed — no name to give
        rows.append(
            {
                "where": f"{origin}:{text.count(chr(10), 0, pos) + 1}",
                "tag": tag,
                "attrs": attrs,
                "wrapped": depth > 0,
                "self_named": tag == "button" and _names_itself(attrs, text, end),
                "raw": " ".join(raw.split())[:110],
            }
        )
    return rows, label_targets


def _sweep() -> list[dict]:
    # Label targets are scoped PER TEMPLATE: each template renders as its own page (or
    # fragment swapped into one), so a `<label for>` in dashboard.html cannot name a
    # control in login.html — a global set would let an id reused across pages satisfy
    # the gate vacuously. Each row carries its own template's declared targets.
    rows: list[dict] = []
    for path in sorted(_TEMPLATES.glob("*.html")):
        found, declared = _controls(path.read_text(encoding="utf-8"), path.name)
        for row in found:
            row["targets"] = declared
        rows += found
    return rows


def _lacks_id_and_name(row: dict) -> bool:
    return row["tag"] in _FIELDS and not (_has(row["attrs"], "id") or _has(row["attrs"], "name"))


def _lacks_accessible_name(row: dict, targets: set[str]) -> bool:
    # `targets` must be the DECLARING template's own label-for set (see _sweep) — never a
    # union across templates, which would let an unrelated page's label name this control.
    attrs = row["attrs"]
    return not (
        _has(attrs, "aria-label")
        or _has(attrs, "aria-labelledby")
        or _has(attrs, "title")
        or row["wrapped"]
        or row["self_named"]
        or bool(_values(attrs, "id") & targets)
        or (attrs.get("type", "").lower() in {"submit", "button"} and "value" in attrs)
    )


# ---- the sweep ---------------------------------------------------------------


def test_every_form_field_has_an_id_or_name():
    rows = _sweep()
    offenders = [f"{r['where']} {r['raw']}" for r in rows if _lacks_id_and_name(r)]
    assert offenders == []


def test_every_control_has_an_accessible_name():
    rows = _sweep()
    offenders = [
        f"{r['where']} {r['raw']}" for r in rows if _lacks_accessible_name(r, r["targets"])
    ]
    assert offenders == []


def test_sweep_actually_inspected_the_templates():
    """Guard the guard: an empty scan would make both sweeps pass vacuously."""
    rows = _sweep()
    assert len(rows) > 100
    assert {r["tag"] for r in rows} >= {"input", "select", "textarea", "button"}


def test_label_targets_do_not_leak_across_templates():
    """A label in one template must not name an id-reusing control in another."""
    labelled = "<label for='shared-id'>Name</label><input id='shared-id'>"
    bare = "<input id='shared-id' name='x'>"
    rows_a, targets_a = _controls(labelled, "page_a")
    rows_b, targets_b = _controls(bare, "page_b")
    # In its own template the labelled control is named; the OTHER template's identical
    # id is not — the cross-template union Greptile flagged would have passed both.
    assert not _lacks_accessible_name(rows_a[0], targets_a)
    assert _lacks_accessible_name(rows_b[0], targets_b)
    assert not _lacks_accessible_name(rows_b[0], targets_a | targets_b)  # the old, wrong way


def test_scanner_flags_a_known_offender():
    """Prove the instrument can produce a positive before trusting its zeroes."""
    bad = "<input type='text' placeholder='bare'><select></select><button></button>"
    rows, targets = _controls(bad, "probe")
    assert [r["tag"] for r in rows if _lacks_id_and_name(r)] == ["input", "select"]
    assert [r["tag"] for r in rows if _lacks_accessible_name(r, targets)] == [
        "input",
        "select",
        "button",
    ]


def test_scanner_ignores_controls_named_only_in_comments():
    """A ``<select>`` mentioned in a comment is prose, not a control."""
    rows, _ = _controls("{# <select> #}<!-- <input> --><script>\n  // <textarea>\n</script>", "c")
    assert rows == []


def test_scanner_survives_alpine_expressions_containing_angle_brackets():
    """``x-show="a > b"`` must not terminate the tag early and hide later attributes."""
    rows, _ = _controls("<input x-show=\"a > b && c < d\" id='ok' aria-label='ok'>", "c")
    assert len(rows) == 1
    assert not _lacks_id_and_name(rows[0])


def test_scanner_pairs_a_bound_label_with_a_bound_id():
    """The config editor labels rows with ``:for``/``:id`` — that pairing is a real name."""
    rows, targets = _controls(
        "<label :for=\"'cfg-' + path\">L</label><input :id=\"'cfg-' + path\" name='n'>", "c"
    )
    assert not _lacks_accessible_name(rows[0], targets)


# ---- the ids actually reach the browser --------------------------------------

_CONFIG_WRITE_ON = "config_write:\n  enabled: true\n"


def _dashboard(write_config, extra: str) -> str:
    client = TestClient(create_app(load_config(write_config(extra))))
    resp = client.get("/")
    assert resp.status_code == 200
    return resp.text


def _project_row(write_config) -> str:
    client = TestClient(create_app(load_config(write_config(""))))
    resp = client.get("/api/projects/alpha/row")
    assert resp.status_code == 200
    return resp.text


def test_dashboard_singleton_controls_render_static_ids(write_config):
    html = _dashboard(write_config, _CONFIG_WRITE_ON)
    for ident in ("project-search", "project-sort", "cfg-filter", "transcript-search"):
        assert f'id="{ident}"' in html
        assert f'name="{ident}"' in html


def test_config_management_confirm_inputs_render_ids(write_config):
    html = _dashboard(write_config, _CONFIG_WRITE_ON)
    for ident in ("cm-mcp-delete-input", "cm-plugin-install-id", "cm-marketplace-add-source"):
        assert f'id="{ident}"' in html


def test_repeated_rows_bind_their_ids_to_a_row_key(write_config):
    """``x-for`` rows must compute the id, or every row would render the same one."""
    html = _dashboard(write_config, _CONFIG_WRITE_ON)
    assert ":id=\"'cm-hook-matcher-' + row.id\"" in html
    assert ":id=\"'cm-settings-env-key-' + row.id\"" in html


def test_project_row_controls_are_scoped_to_the_project(write_config):
    """Rows repeat per project, so their ids carry the project name to stay unique."""
    html = _project_row(write_config)
    for ident in ("lcloud-alpha", "trust-ok-alpha", "bypass-typed-alpha", "cmd-text-alpha"):
        assert f'id="{ident}"' in html
