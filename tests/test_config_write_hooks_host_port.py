"""The http-hook URL masker's host check (code-scanning alert 184).

``_is_host_port`` replaced a regular expression that CodeQL flagged as polynomial. It
must accept exactly what that expression accepted, so the masker's decision to mask a
URL whole is unchanged.
"""

from __future__ import annotations

import itertools
import re

import pytest

from clauster.config_write_hooks import _is_host_port

#: The expression the function replaced, kept only as the test oracle.
_OLD = re.compile(r"(\[[^\]]*\]|[^:\[\]]*)(:\d*)?")

#: "\u0663" is a decimal digit; "\u00b2" (isdigit only) and "\u00bd" (isnumeric only) are
#: not, so a check that swapped isdecimal for either fails here.
_ALPHABET = ["[", "]", ":", "1", "a", ".", "@", "\n", "\u0663", "\u00b2", "\u00bd", "-"]


@pytest.mark.parametrize("length", range(6))
def test_matches_the_old_expression_on_every_short_string(length: int) -> None:
    for chars in itertools.product(_ALPHABET, repeat=length):
        host = "".join(chars)
        assert _is_host_port(host) == bool(_OLD.fullmatch(host)), repr(host)


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("example.com", True),
        ("example.com:8443", True),
        ("example.com:", True),
        ("[::1]:443", True),
        ("[::1]", True),
        ("[::1", False),
        ("example.com:84a", False),
        ("ex[ample.com", False),
        ("[::1]x", False),
        ("a:b:c", False),
    ],
)
def test_known_hosts(host: str, ok: bool) -> None:
    assert _is_host_port(host) is ok


def test_long_bracket_run_is_answered() -> None:
    # The flagged input shape: "[" followed by many "[". A linear scan answers it.
    assert _is_host_port("[" * 200_000) is False
    assert _is_host_port("[" + "[" * 200_000 + "]") is True
