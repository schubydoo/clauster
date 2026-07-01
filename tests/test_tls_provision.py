"""Tests for the self-signed TLS provisioner (tls_provision.py).

Key-material never leaves the tmp_path; no real TLS material is served; no
ports are bound.  The conftest isolates HOME so state_dir defaults don't touch
the live account.
"""

from __future__ import annotations

import datetime
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from clauster.tls_provision import (
    _atomic_write,
    _generate,
    _san_set,
    _tls_dir,
    cert_needs_regen,
    generate_self_signed,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gen_cert(state_dir: Path, hostnames: list[str]) -> tuple[Path, Path]:
    """Generate a real cert+key pair and return (cert_path, key_path)."""
    return generate_self_signed(state_dir, hostnames)


# ---------------------------------------------------------------------------
# _san_set
# ---------------------------------------------------------------------------


def test_san_set_normalises_case():
    assert _san_set(["MyHost", "MYHOST"]) == frozenset({"myhost"})


def test_san_set_skips_empty():
    assert _san_set(["host", "", "  "]) == frozenset({"host", "  "})


# ---------------------------------------------------------------------------
# _tls_dir
# ---------------------------------------------------------------------------


def test_tls_dir_creates_subdir(tmp_path):
    d = _tls_dir(tmp_path)
    assert d == tmp_path / "tls"
    assert d.is_dir()


def test_tls_dir_idempotent(tmp_path):
    _tls_dir(tmp_path)
    _tls_dir(tmp_path)  # should not raise
    assert (tmp_path / "tls").is_dir()


# ---------------------------------------------------------------------------
# _atomic_write
# ---------------------------------------------------------------------------


def test_atomic_write_creates_file_with_mode(tmp_path):
    dest = tmp_path / "key.pem"
    _atomic_write(dest, b"KEY", mode=0o600)
    assert dest.read_bytes() == b"KEY"
    if os.name == "posix":
        m = stat.S_IMODE(dest.stat().st_mode)
        assert m == 0o600


def test_atomic_write_no_temp_file_left_on_success(tmp_path):
    dest = tmp_path / "cert.pem"
    _atomic_write(dest, b"CERT", mode=0o644)
    assert not (tmp_path / "cert.pem.tmp").exists()


def test_atomic_write_cleans_up_temp_on_failure(tmp_path):
    dest = tmp_path / "key.pem"
    # Make the destination's parent directory read-only to trigger rename failure.
    # Instead, patch os.write to raise after open so we test the cleanup branch.
    with patch("clauster.tls_provision.os.write", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            _atomic_write(dest, b"KEY", mode=0o600)
    assert not (tmp_path / "key.pem.tmp").exists()


# ---------------------------------------------------------------------------
# cert_needs_regen
# ---------------------------------------------------------------------------


def test_cert_needs_regen_missing_cert(tmp_path):
    assert cert_needs_regen(tmp_path / "nope.crt", ["host"])


def test_cert_needs_regen_corrupt_cert(tmp_path):
    bad = tmp_path / "bad.crt"
    bad.write_bytes(b"not a cert")
    assert cert_needs_regen(bad, ["host"])


def test_cert_needs_regen_fresh_cert_is_false(tmp_path):
    cert_path, _ = _gen_cert(tmp_path, ["localhost"])
    assert not cert_needs_regen(cert_path, ["localhost"])


def test_cert_needs_regen_san_change(tmp_path):
    cert_path, _ = _gen_cert(tmp_path, ["localhost"])
    # Same cert, but now we want an extra hostname — must regen.
    assert cert_needs_regen(cert_path, ["localhost", "192.168.1.1"])


def test_cert_needs_regen_near_expiry(tmp_path, monkeypatch):
    cert_path, _ = _gen_cert(tmp_path, ["localhost"])
    # Advance "now" to within the renewal window (past _CERT_LIFETIME_DAYS - _RENEW_BEFORE_DAYS).
    from clauster import tls_provision

    future = datetime.datetime.now(tz=datetime.UTC) + datetime.timedelta(
        days=tls_provision._CERT_LIFETIME_DAYS - tls_provision._RENEW_BEFORE_DAYS + 1
    )
    # Patch only datetime.datetime.now so the rest of the real datetime module is intact.
    monkeypatch.setattr(
        "clauster.tls_provision.datetime",
        type(
            "FakeDT",
            (),
            {
                "datetime": type(
                    "FakeDatetime",
                    (),
                    {
                        "now": staticmethod(lambda tz=None: future),
                        "timedelta": datetime.timedelta,
                    },
                ),
                "timedelta": datetime.timedelta,
                "timezone": datetime.timezone,
                "UTC": datetime.UTC,
            },
        ),
    )
    assert cert_needs_regen(cert_path, ["localhost"])


# ---------------------------------------------------------------------------
# generate_self_signed — happy path
# ---------------------------------------------------------------------------


def test_generate_self_signed_creates_cert_and_key(tmp_path):
    cert_path, key_path = generate_self_signed(tmp_path, ["localhost"])
    assert cert_path.is_file()
    assert key_path.is_file()


def test_generate_self_signed_key_is_0600(tmp_path):
    _, key_path = generate_self_signed(tmp_path, ["localhost"])
    if os.name == "posix":
        m = stat.S_IMODE(key_path.stat().st_mode)
        assert m == 0o600


def test_generate_self_signed_cert_paths_are_absolute(tmp_path):
    cert_path, key_path = generate_self_signed(tmp_path, ["localhost"])
    assert cert_path.is_absolute()
    assert key_path.is_absolute()


def test_generate_self_signed_cert_under_state_tls(tmp_path):
    cert_path, key_path = generate_self_signed(tmp_path, ["localhost"])
    assert cert_path.parent == (tmp_path / "tls").resolve()
    assert key_path.parent == (tmp_path / "tls").resolve()


def test_generate_self_signed_ip_address_san(tmp_path):
    """IP-address SANs must be encoded as IPAddress, not DNSName."""
    cert_path, _ = generate_self_signed(tmp_path, ["192.168.1.10"])
    from cryptography import x509  # type: ignore[import-not-found]

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    ip_values = [str(n.value) for n in san.value if isinstance(n, x509.IPAddress)]
    assert "192.168.1.10" in ip_values


def test_generate_self_signed_hostname_san(tmp_path):
    cert_path, _ = generate_self_signed(tmp_path, ["myhost.local"])
    from cryptography import x509  # type: ignore[import-not-found]

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    dns_values = [n.value for n in san.value if isinstance(n, x509.DNSName)]
    assert "myhost.local" in dns_values


def test_generate_self_signed_cn_is_first_hostname(tmp_path):
    cert_path, _ = generate_self_signed(tmp_path, ["firsthost", "secondhost"])
    from cryptography import x509  # type: ignore[import-not-found]
    from cryptography.x509.oid import NameOID  # type: ignore[import-not-found]

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    cn = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    assert cn == "firsthost"


def test_generate_self_signed_valid_pem_cert(tmp_path):
    """The written cert must parse as a valid PEM X.509 certificate."""
    cert_path, _ = generate_self_signed(tmp_path, ["localhost"])
    from cryptography import x509  # type: ignore[import-not-found]

    cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
    assert cert.serial_number > 0


def test_generate_self_signed_valid_pem_key(tmp_path):
    """The written key must parse as a valid PEM private key."""
    _, key_path = generate_self_signed(tmp_path, ["localhost"])
    from cryptography.hazmat.primitives.serialization import (
        load_pem_private_key,  # type: ignore[import-not-found]
    )

    key = load_pem_private_key(key_path.read_bytes(), password=None)
    assert key.key_size == 2048


# ---------------------------------------------------------------------------
# generate_self_signed — reuse (idempotency)
# ---------------------------------------------------------------------------


def test_generate_self_signed_reuses_current_cert(tmp_path):
    cert_path1, _ = generate_self_signed(tmp_path, ["localhost"])
    mtime1 = cert_path1.stat().st_mtime
    cert_path2, _ = generate_self_signed(tmp_path, ["localhost"])
    mtime2 = cert_path2.stat().st_mtime
    assert mtime1 == mtime2  # file not rewritten


def test_generate_self_signed_regens_on_san_change(tmp_path):
    cert_path, _ = generate_self_signed(tmp_path, ["localhost"])
    mtime1 = cert_path.stat().st_mtime
    # Add a new hostname — must regenerate.
    cert_path2, _ = generate_self_signed(tmp_path, ["localhost", "extra.local"])
    mtime2 = cert_path2.stat().st_mtime
    assert mtime2 != mtime1


# ---------------------------------------------------------------------------
# generate_self_signed — fail-closed
# ---------------------------------------------------------------------------


def test_generate_self_signed_empty_hostnames_raises(tmp_path):
    with pytest.raises(ValueError, match="at least one hostname"):
        generate_self_signed(tmp_path, [])


def test_generate_self_signed_missing_cryptography_raises(tmp_path):
    with patch("clauster.tls_provision._imports", side_effect=RuntimeError("no cryptography")):
        with pytest.raises(RuntimeError, match="no cryptography"):
            generate_self_signed(tmp_path, ["localhost"])


# ---------------------------------------------------------------------------
# Key-material safety: key bytes must never appear in exception messages
# ---------------------------------------------------------------------------


def test_generate_self_signed_key_bytes_not_in_errors(tmp_path):
    """A disk-write failure must not leak key bytes in the exception message."""
    captured_exc: list[Exception] = []

    original_atomic_write = _atomic_write

    def bad_write(dest, data, mode):
        if mode == 0o600:  # the key write
            raise OSError("disk full")
        return original_atomic_write(dest, data, mode)

    with patch("clauster.tls_provision._atomic_write", side_effect=bad_write):
        try:
            _generate(tmp_path / "cert.crt", tmp_path / "key.key", ["localhost"])
        except OSError as exc:
            captured_exc.append(exc)

    assert captured_exc, "expected OSError"
    # The exception message must not contain PEM key material.
    msg = str(captured_exc[0])
    assert "BEGIN" not in msg
    assert "PRIVATE" not in msg
