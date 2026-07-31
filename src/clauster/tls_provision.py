"""Self-signed TLS certificate provisioner for Clauster.

Generates a self-signed RSA-2048 cert+key pair under ``state_dir/tls/`` when
``tls.provision = self-signed``.  The cert is regenerated on expiry (within
``_RENEW_BEFORE_DAYS`` of expiry) or when the SAN set changes (a hostname was
added or removed).  Reordering ``hostnames`` changes the CN but not the SAN set,
so no regen fires — intentional, since modern TLS clients verify against the SANs,
not the CN.

Key-material handling guarantees
---------------------------------
- Private key written 0600 (owner-read/write only) via an ``O_CREAT | O_EXCL``
  create (stale temp unlinked first) + ``fchmod`` — so a pre-existing/pre-planted
  temp file can never leak the key through the wrong mode, and the predictable
  temp path can't be symlink-followed.  Never logged, never serialised to any API
  response, never carried in exception messages.
- The only paths that leave this module are filesystem paths — not key bytes.
- Serial numbers are time-based (microseconds since epoch) so no persistent
  state file is needed.
- IP SANs are canonicalised on both write and read-back so a non-canonical input
  (``2001:0db8::1``) doesn't force a spurious regen (and key churn) every boot.
- Generation uses the ``cryptography`` package (pure-Python); no shell-out to
  ``openssl``.

Scope
-----
v1 = self-signed only.  ACME / Let's Encrypt is deferred to issue 774.
"""

from __future__ import annotations

import contextlib
import datetime
import ipaddress
import logging
import os
from pathlib import Path

_log = logging.getLogger("clauster.tls_provision")

# Lifetime for a freshly generated cert.
_CERT_LIFETIME_DAYS = 825  # just under the 2-year browser soft-cap; self-signed has
# no formal CA/policy cap, but 825 days is a widely-used practical ceiling.

# Begin regeneration this many days before expiry.
_RENEW_BEFORE_DAYS = 30


def _imports():
    """Return the cryptography sub-modules used by this module.

    Deferred so that the module can be imported without ``cryptography`` installed
    (e.g. in tests that stub this function).  A missing package surfaces as a clear
    ``RuntimeError`` at call time rather than an ``ImportError`` at module import.
    """
    try:
        from cryptography import x509  # type: ignore[import-not-found]
        from cryptography.hazmat.primitives import (  # type: ignore[import-not-found]
            hashes,
            serialization,
        )
        from cryptography.hazmat.primitives.asymmetric import rsa  # type: ignore[import-not-found]
        from cryptography.x509.oid import NameOID  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "tls.provision = self-signed requires the 'cryptography' package — "
            "install it with: pip install cryptography  (or reinstall: pip install clauster)"
        ) from exc
    return x509, hashes, serialization, rsa, NameOID


def _tls_dir(state_dir: Path) -> Path:
    """Return (and create) the TLS storage sub-directory under state_dir."""
    tls = state_dir / "tls"
    tls.mkdir(parents=True, exist_ok=True)
    return tls


def _san_set(hostnames: list[str]) -> frozenset[str]:
    """Normalise the SAN list to a comparable frozenset (canonical, de-duped).

    IP addresses are canonicalised through ``ipaddress`` so a non-canonical input
    (``2001:0db8::1`` / ``fe80::0:1``) compares equal to the cert's stored
    canonical form (``2001:db8::1`` / ``fe80::1``).  Without this, ``cert_needs_regen``
    would flag a mismatch on every startup and churn a fresh private key each boot.
    Non-IP entries are lower-cased DNS names (matching how the read-back compares
    ``DNSName`` values).
    """
    out: set[str] = set()
    for raw in hostnames:
        h = raw.strip()
        if not h:
            continue
        try:
            out.add(str(ipaddress.ip_address(h)).lower())
        except ValueError:
            out.add(h.lower())
    return frozenset(out)


def cert_needs_regen(cert_path: Path, hostnames: list[str]) -> bool:
    """Return True when the cert at *cert_path* needs to be regenerated.

    Triggers:

    - cert file missing or unreadable / unparseable
    - within ``_RENEW_BEFORE_DAYS`` of expiry (or already expired)
    - the requested SAN set differs from the cert's current SAN set
    """
    if not cert_path.is_file():
        return True
    try:
        from cryptography import x509 as _x509  # type: ignore[import-not-found]

        cert = _x509.load_pem_x509_certificate(cert_path.read_bytes())
    except Exception:  # noqa: BLE001 - unreadable/corrupt cert → regenerate
        return True

    # Expiry check.
    now_utc = datetime.datetime.now(tz=datetime.UTC)
    renew_after = cert.not_valid_after_utc - datetime.timedelta(days=_RENEW_BEFORE_DAYS)
    if now_utc >= renew_after:
        return True

    # SAN change check.
    requested = _san_set(hostnames)
    try:
        san_ext = cert.extensions.get_extension_for_class(_x509.SubjectAlternativeName)
        existing: frozenset[str] = frozenset(
            n.value.lower() if isinstance(n, _x509.DNSName) else str(n.value).lower()
            for n in san_ext.value
            if isinstance(n, (_x509.DNSName, _x509.IPAddress))
        )
    except _x509.ExtensionNotFound:
        existing = frozenset()
    return requested != existing


def generate_self_signed(
    state_dir: Path,
    hostnames: list[str],
) -> tuple[Path, Path]:
    """Generate (or reuse) a self-signed cert+key under ``state_dir/tls/``.

    Returns ``(cert_path, key_path)`` as resolved absolute ``Path`` objects.
    Regenerates only when the existing cert is missing, expired / near-expiry,
    or the SAN set changed.  The private key is always written 0600.

    Parameters
    ----------
    state_dir:
        The Clauster state directory (expanded + resolved by the caller).
    hostnames:
        List of hostnames / IP-address strings to include as Subject Alternative
        Names.  The first entry becomes the Common Name.  Must be non-empty.

    Raises
    ------
    ValueError
        If ``hostnames`` is empty (there is no sensible default CN).
    RuntimeError
        If the ``cryptography`` package is not installed.
    OSError
        If writing the cert or key to disk fails.
    """
    if not hostnames:
        raise ValueError(
            "tls.provision = self-signed requires at least one hostname / IP in "
            "tls.hostnames so the certificate has a valid CN and SAN."
        )

    tls_dir = _tls_dir(state_dir)
    cert_path = tls_dir / "self-signed.crt"
    key_path = tls_dir / "self-signed.key"

    if not cert_needs_regen(cert_path, hostnames) and key_path.is_file():
        _log.debug("tls: self-signed cert is current, reusing %s", cert_path)
        return cert_path.resolve(), key_path.resolve()

    _log.info("tls: generating self-signed cert for %s", hostnames)
    _generate(cert_path, key_path, hostnames)
    _log.info("tls: self-signed cert written to %s", cert_path)
    return cert_path.resolve(), key_path.resolve()


def _generate(cert_path: Path, key_path: Path, hostnames: list[str]) -> None:
    """Write a fresh self-signed RSA-2048 cert+key pair to disk.

    Private key: written 0600 (owner-only).  Cert: 0644.  Both are written
    atomically via a sibling temp file so a crash between the two writes leaves
    the cert absent and the next start regenerates cleanly (rather than a cert
    pointing at a missing/stale key).  Key bytes are never echoed to logs or
    exception messages.
    """
    x509, hashes, serialization, rsa, NameOID = _imports()

    # Generate RSA-2048 private key (2048 is the minimum widely accepted; 4096
    # is slower to handshake on constrained hardware for no security benefit at
    # LAN-threat levels).
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    cn = hostnames[0]
    now_utc = datetime.datetime.now(tz=datetime.UTC)
    not_after = now_utc + datetime.timedelta(days=_CERT_LIFETIME_DAYS)

    # X.509 serial — time-based (microseconds since epoch) gives uniqueness
    # within a deployment without a persistent counter file.  Must be a positive
    # integer ≤ 20 bytes (RFC 5280 §4.1.2.2); microseconds-since-epoch is
    # ~56 bits, well within the 159-bit cap.
    serial = int(now_utc.timestamp() * 1_000_000) & ((1 << 159) - 1)

    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    # Build the SAN extension: each entry is a DNS name or an IP address.
    san_entries: list = []
    for h in hostnames:
        try:
            addr = ipaddress.ip_address(h)
            san_entries.append(x509.IPAddress(addr))
        except ValueError:
            san_entries.append(x509.DNSName(h))

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)  # self-signed: issuer == subject
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now_utc)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    # Write key first (0600), then cert — so if we crash between the two
    # writes the cert is absent and the next start regenerates cleanly.
    _atomic_write(key_path, key_pem, mode=0o600)
    _atomic_write(cert_path, cert_pem, mode=0o644)


def _atomic_write(dest: Path, data: bytes, mode: int) -> None:
    """Write *data* to *dest* atomically, creating it with *mode* permissions.

    Uses a sibling temp file + rename so neither a partial write nor a crash
    leaves a half-written file.  On POSIX the private-key file (mode=0600) is never
    world-readable, even transiently; on Windows the mode bits are inert (no ``fchmod``)
    and the key file inherits the ACL of its parent directory.  Key bytes are in *data*
    — the caller must not echo them.

    Permission correctness is defended two ways so a **pre-existing** temp file
    (a crashed prior run whose perms drifted, or one pre-planted by a local user)
    can never leak the key through the wrong mode:

    - ``O_TRUNC`` applies ``mode`` only on *creation*; a pre-existing 0644 temp
      would keep 0644 and the rename would carry that onto the key.  So we
      ``unlink`` any stale temp first, then open with ``O_CREAT | O_EXCL`` — the
      create is always fresh (``mode`` always applies), and ``O_EXCL`` also closes
      the symlink-follow vector on the predictable temp path.
    - ``os.fchmod(fd, mode)`` re-asserts the mode on the fd before writing, in
      case a restrictive process ``umask`` masked bits off the ``open`` mode.

    The final swap uses ``os.replace`` (not ``Path.rename``) so it atomically
    overwrites an existing destination on Windows too — the cert-regeneration path.
    """
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        # Remove any stale/pre-planted temp so O_EXCL always creates fresh (applying
        # `mode`) and refuses to follow a symlink planted on the predictable path.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(str(tmp))
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        try:
            # Belt-and-suspenders: umask can mask bits off the open() mode, so
            # re-assert the intended mode on the fd before any key bytes land.
            # os.fchmod is POSIX-only; the mode bits are meaningless on Windows.
            if hasattr(os, "fchmod"):  # pragma: skip-on-win
                os.fchmod(fd, mode)
            os.write(fd, data)
        finally:
            os.close(fd)
        # os.replace atomically overwrites an existing dest on BOTH POSIX and
        # Windows (Path.rename / os.rename raise on Windows if dest exists — which
        # is exactly the cert-regeneration path: a second write over self-signed.key).
        os.replace(str(tmp), str(dest))
    except BaseException:
        # Best-effort cleanup of the temp file on any failure.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise
