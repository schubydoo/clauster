"""No ``x-show`` on an element that carries a Tabler display utility (TASK-493).

Tabler's ``d-flex``, ``d-block``, ``d-grid`` and the other ``d-*`` display utilities set
``display: … !important``. Alpine's ``x-show`` hides an element with an inline
``display: none``, which loses to ``!important``, so such an element never hides. The
fix is to put ``x-show`` on a plain wrapper and keep the ``d-*`` class on a child.

``test_no_xshow_element_carries_important_display_utility`` in ``test_app.py`` scans the
rendered dashboard. This test scans the template sources instead, so a Jinja branch that
no test render turns on is covered too. It uses the stdlib HTML parser, so an attribute
value that holds ``>`` (``x-show="n > 0"``, an arrow function) cannot end a tag early.
There is no allowlist: every site is fixed.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "src" / "clauster" / "templates"

# Every Bootstrap/Tabler display utility, with its optional breakpoint or print infix.
_DISPLAY = re.compile(
    r"(?<![\w-])d-(?:(?:sm|md|lg|xl|xxl|print)-)?"
    r"(?:none|inline|inline-block|block|grid|inline-grid|table|table-row|table-cell|flex|"
    r"inline-flex)(?![\w-])"
)
_CLASS_ATTRS = ("class", ":class", "x-bind:class")


class _XShowScanner(HTMLParser):
    """Collect each start tag that has ``x-show`` and a ``d-*`` display class."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.xshow_count = 0
        self.hits: list[tuple[int, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name for name, _ in attrs}
        if "x-show" not in names:
            return
        self.xshow_count += 1
        for name, value in attrs:
            if name in _CLASS_ATTRS and value and (m := _DISPLAY.search(value)):
                self.hits.append((self.getpos()[0], tag, m.group(0)))

    handle_startendtag = handle_starttag


def _scan(source: str) -> _XShowScanner:
    scanner = _XShowScanner()
    scanner.feed(source)
    scanner.close()
    return scanner


def test_scanner_flags_a_display_utility_beside_x_show() -> None:
    # Positive control: the scanner must see the bug shape, including after an attribute
    # value that holds ">", in a bound :class, and with a breakpoint infix.
    sample = (
        '<div x-show="n > 0 && xs.some((x) => x)" class="d-flex gap-2"></div>\n'
        "<span :class=\"open ? 'd-block' : ''\" x-show=\"open\"></span>\n"
        '<input x-show="a" class="form-control d-md-inline-flex">\n'
    )
    scanner = _scan(sample)
    assert [(line, tag, cls) for line, tag, cls in scanner.hits] == [
        (1, "div", "d-flex"),
        (2, "span", "d-block"),
        (3, "input", "d-md-inline-flex"),
    ]


def test_scanner_accepts_a_plain_wrapper() -> None:
    sample = (
        '<div x-show="open"><div class="d-flex gap-2"></div></div>\n'
        '<div class="d-flex"></div>\n'
        '<span x-show="open" class="badge d-none-ish flex-wrap"></span>\n'
    )
    assert _scan(sample).hits == []


def test_no_template_puts_x_show_on_a_display_utility() -> None:
    files = sorted(TEMPLATES.rglob("*.html"))
    assert files, f"no templates found under {TEMPLATES}"
    total = 0
    offenders = []
    for path in files:
        scanner = _scan(path.read_text(encoding="utf-8"))
        total += scanner.xshow_count
        rel = path.relative_to(TEMPLATES).as_posix()
        offenders += [f"{rel}:{line} <{tag}> {cls}" for line, tag, cls in scanner.hits]
    # The scan must have seen the templates' x-show elements, or a pass means nothing.
    assert total > 100, f"only {total} x-show elements found; the scanner is not reading"
    assert offenders == [], (
        "x-show on an element with a Tabler !important display class never hides it; "
        "move x-show to a plain wrapper element:\n" + "\n".join(offenders)
    )
