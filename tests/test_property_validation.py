"""Property-based tests for the security-validation gates (issue #356).

The Atheris fuzzers (``fuzz/``) hunt for *crashes* on adversarial input in the
slow scheduled loop. These Hypothesis tests assert the *positive invariants* of
the same gates in the fast default suite, so a regression that quietly weakens a
gate (rather than crashing it) fails immediately:

* ``discovery.is_valid_project_name`` — an accepted name is always a single,
  in-root path component (no traversal escapes ``projects_root``).
* ``provisioning.validate_clone_url`` — a non-allowlisted scheme always raises,
  and a host resolving to a private/loopback/link-local address is always
  blocked (SSRF guard). DNS is monkeypatched so the test stays offline and
  deterministic, mirroring ``tests/test_provisioning.py``.
* ``redact`` — sanitized output never still carries a bearer-equivalent
  ``env_``/``session_``/``cse_`` id or a bare UUID.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from clauster import redact
from clauster.config import CloneConfig
from clauster.discovery import PROJECT_NAME_RE, is_valid_project_name
from clauster.provisioning import (
    BlockedCloneHost,
    InvalidCloneUrl,
    validate_clone_url,
)

# ----- is_valid_project_name: no accepted name escapes projects_root ----

# Mirror the production matcher's character class so the "accepted" branch is
# actually exercised often; Hypothesis still also feeds wholly arbitrary text
# (below) to probe the reject branch.
_NAME_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"


@given(name=st.text(alphabet=_NAME_CHARS, min_size=0, max_size=80))
def test_valid_name_never_escapes_projects_root(name: str) -> None:
    """An accepted name resolves to a direct, in-root child of projects_root."""
    root = Path("/srv/projects").resolve()
    accepted = is_valid_project_name(name)
    if accepted:
        # An accepted name must contain no separators or traversal, and the
        # joined-then-resolved path must stay strictly inside projects_root.
        assert "/" not in name and "\\" not in name and ".." not in name
        resolved = (root / name).resolve()
        assert resolved.parent == root
        assert resolved != root


@given(name=st.text(min_size=0, max_size=80))
def test_name_acceptance_matches_single_component_invariant(name: str) -> None:
    """Acceptance is exactly the single-safe-component regex — nothing wider."""
    accepted = is_valid_project_name(name)
    # Deliberately circular against the *current* implementation: this pins the
    # contract so a future refactor that reimplements is_valid_project_name
    # without delegating to PROJECT_NAME_RE would diverge here and fail. The
    # independent structural checks below are what verify the safety property.
    assert accepted == (PROJECT_NAME_RE.fullmatch(name) is not None)
    if accepted:
        # The defining safety property: a sole path segment, 1..64 chars, no
        # separator / parent-ref / NUL that a join could turn into traversal.
        assert 1 <= len(name) <= 64
        assert "/" not in name and "\\" not in name
        assert "\x00" not in name
        assert name not in ("", ".", "..")


@pytest.mark.parametrize(
    "danger",
    [
        "..",
        ".",
        "",
        "../etc",
        "a/b",
        "a\\b",
        "/abs",
        "foo/..",
        "\x00",
        "with space",
        "a" * 65,
        "name;rm -rf",
        "$(whoami)",
        "..%2f",
    ],
)
def test_traversal_and_injection_names_rejected(danger: str) -> None:
    """Known traversal / injection / oversize names are always rejected."""
    assert not is_valid_project_name(danger)


# ----- validate_clone_url: scheme allowlist + SSRF IP block ------------

# A function-scoped monkeypatch applies across every Hypothesis example in the
# test body, which is what we want: every generated case resolves through the
# same stubbed DNS. Suppress the function-scoped-fixture health check because the
# fixture is intentionally set up once for the whole property, not per example.
_PROP = settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])


def _patch_resolve(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    """Make socket.getaddrinfo (as used by validate_clone_url) return one IP."""
    monkeypatch.setattr(
        "clauster.provisioning.socket.getaddrinfo",
        lambda host, port, *a, **k: [(0, 0, 0, "", (ip, port or 0))],
    )


# Schemes that are never on the default allowlist (https/ssh).
_BAD_SCHEMES = st.sampled_from(
    ["file", "ftp", "gopher", "data", "javascript", "http", "git", "smb", "dict", "ldap"]
)


@_PROP
@given(scheme=_BAD_SCHEMES, host=st.from_regex(r"[a-z]{1,12}\.example\.com", fullmatch=True))
def test_non_allowlisted_scheme_always_rejected(
    scheme: str, host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any scheme outside the allowlist raises InvalidCloneUrl without resolving DNS."""
    # Enforce (not just describe) the ordering invariant: validate_clone_url must
    # reject a disallowed scheme BEFORE it ever resolves the host. That ordering is
    # SSRF-relevant — a bad-scheme URL should never trigger a DNS lookup — so fail
    # loudly if getaddrinfo is reached at all.
    monkeypatch.setattr(
        "clauster.provisioning.socket.getaddrinfo",
        lambda *_a, **_k: pytest.fail("getaddrinfo must not run for a disallowed scheme"),
    )
    with pytest.raises(InvalidCloneUrl):
        validate_clone_url(f"{scheme}://{host}/repo.git", CloneConfig())


# Generate addresses across the always-blocked / private space.
_PRIVATE_IPS = st.one_of(
    st.ip_addresses(v=4).filter(
        lambda a: (
            a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_multicast
        )  # _ip_blocked blocks multicast too
    ),
    st.sampled_from(
        [
            ipaddress.ip_address("127.0.0.1"),
            ipaddress.ip_address("10.0.0.5"),
            ipaddress.ip_address("192.168.1.1"),
            ipaddress.ip_address("172.16.0.9"),
            ipaddress.ip_address("169.254.0.1"),  # link-local
            # _EXTRA_PRIVATE_NETS ranges that is_private does NOT flag but
            # provisioning._ip_blocked still blocks — generated explicitly so the
            # property exercises them:
            ipaddress.ip_address("100.64.0.1"),  # CGNAT / Tailscale (100.64/10)
            ipaddress.ip_address("198.18.0.1"),  # benchmarking (198.18/15)
            ipaddress.ip_address("192.0.0.1"),  # IETF protocol (192.0.0/24)
            ipaddress.ip_address("::1"),  # IPv6 loopback
            ipaddress.ip_address("fd00::1"),  # IPv6 ULA (private)
            ipaddress.ip_address("::ffff:127.0.0.1"),  # IPv4-mapped loopback
        ]
    ),
)


@_PROP
@given(
    ip=_PRIVATE_IPS,
    host=st.from_regex(r"[a-z]{1,12}\.example\.com", fullmatch=True),
    scheme=st.sampled_from(["https", "ssh"]),
)
def test_private_or_loopback_host_always_blocked(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    host: str,
    scheme: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host resolving to a private/loopback/link-local address is always blocked."""
    _patch_resolve(monkeypatch, str(ip))
    with pytest.raises(BlockedCloneHost):
        validate_clone_url(f"{scheme}://{host}/repo.git", CloneConfig())


# ----- redact: output never carries a UUID or env_/session_/cse_ id ----

# Reference the production patterns directly rather than hardcoding copies, so a
# tightening of redact._ID_RE / _UUID_RE can never leave these checks stale.
_LEAK_ID = redact._ID_RE
_LEAK_UUID = redact._UUID_RE

# Building blocks that should always be masked, interleaved with arbitrary text.
_TOKENS = st.one_of(
    st.from_regex(r"(env|session|cse)_[A-Za-z0-9]{6,20}", fullmatch=True),
    st.from_regex(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        fullmatch=True,
    ),
    st.text(max_size=40),
)


@settings(max_examples=300)
@given(parts=st.lists(_TOKENS, max_size=8))
def test_sanitize_line_never_leaks_id_or_uuid(parts: list[str]) -> None:
    """sanitize_line output contains no bare env_/session_/cse_ id and no UUID."""
    line = " ".join(parts)
    out = redact.sanitize_line(line)
    assert not _LEAK_ID.search(out), f"id leak: {out!r}"
    assert not _LEAK_UUID.search(out), f"uuid leak: {out!r}"


@settings(max_examples=300)
@given(text=st.text(max_size=200))
def test_redact_ids_idempotent_and_clean(text: str) -> None:
    """redact_ids removes ids/UUIDs and re-applying it changes nothing further."""
    once = redact.redact_ids(text)
    assert not _LEAK_ID.search(once)
    assert not _LEAK_UUID.search(once)
    assert redact.redact_ids(once) == once  # idempotent
