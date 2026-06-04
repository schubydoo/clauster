"""Authentication primitives for the v0.2 auth foundation (spec §4, D12/D13).

Pure functions + small helpers — deliberately free of any FastAPI/Starlette
import so the security-sensitive logic is unit-testable in isolation. The
web wiring (middleware, routes, cookie handling) lives in ``app.py``.

Three trust paths:
  - password login  -> signed-cookie session  (``issue_session`` / ``read_session``)
  - reverse proxy    -> peer-IP allowlist + HMAC-signed header
                        (``peer_trusted`` / ``verify_proxy_hmac``)
  - cross-site guard -> strict Origin allowlist
                        (``build_allowed_origins`` / ``normalize_origin``)
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import _LOOPBACK_HOSTS, ClausterConfig

_SESSION_SALT = "clauster-session"
# A real argon2id hash used to keep verify timing constant when no password is
# configured / the attempt is empty — defends against a "no password set" oracle.
_DUMMY_HASH = PasswordHasher().hash("clauster-dummy-do-not-use")


# ----- session secret -----------------------------------------------------


def load_or_create_secret(state_dir: Path) -> bytes:
    """Return the session-signing secret.

    ``CLAUSTER_SESSION_SECRET`` wins (lets ephemeral-FS deploys keep sessions
    across restarts). Otherwise read/create ``state_dir/session.secret`` with
    ``O_CREAT|O_EXCL`` at mode 0600 so it's never briefly world-readable.
    """
    env = os.environ.get("CLAUSTER_SESSION_SECRET")
    if env:
        raw = env.encode("utf-8")
        if len(raw) < 32:
            raise ValueError(
                "CLAUSTER_SESSION_SECRET must be at least 32 bytes; "
                'generate one with `python -c "import secrets; print(secrets.token_hex(32))"`.'
            )
        return raw
    state_dir = state_dir.expanduser()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)  # holds the secret
    path = state_dir / "session.secret"
    # O_BINARY (Windows-only; 0 on POSIX) keeps os.write from translating any
    # 0x0A byte in the random secret into 0x0D 0x0A — which would corrupt ~12% of
    # secrets and break session persistence across restarts on Windows.
    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return path.read_bytes()
    try:
        secret = secrets.token_bytes(32)
        os.write(fd, secret)
        return secret
    finally:
        os.close(fd)


# ----- passwords -----------------------------------------------------------


def make_hasher() -> PasswordHasher:
    """Build an argon2id password hasher with the library's default cost params."""
    return PasswordHasher()  # library defaults: argon2id, sane time/memory cost


def hash_password(hasher: PasswordHasher, plaintext: str) -> str:
    """Hash ``plaintext`` into an argon2id encoded string for ``auth.password_hash``."""
    return hasher.hash(plaintext)


def verify_password(hasher: PasswordHasher, stored_hash: str | None, attempt: str) -> bool:
    """Constant-time-ish password check.

    Always runs a real argon2 verify (against a dummy hash when none is
    configured) so the absence of a password isn't observable via timing, and
    never authenticates against the dummy.
    """
    target = stored_hash or _DUMMY_HASH
    try:
        hasher.verify(target, attempt)
    except (VerificationError, InvalidHashError):
        return False
    return stored_hash is not None


# ----- sessions ------------------------------------------------------------


def make_serializer(secret: bytes) -> URLSafeTimedSerializer:
    """Build the timed serializer used to sign/verify session cookies."""
    return URLSafeTimedSerializer(secret, salt=_SESSION_SALT)


def issue_session(serializer: URLSafeTimedSerializer, user: str, epoch: int = 0) -> str:
    """Sign a session cookie carrying the user and the issuing ``epoch``.

    The epoch lets ``read_session`` reject cookies issued before the last
    server-side revocation (see ``bump_epoch``) — turning logout into actual
    revocation rather than a client-side cookie delete.
    """
    return serializer.dumps({"u": user, "e": epoch})


def read_session(
    serializer: URLSafeTimedSerializer,
    token: str | None,
    max_age: int,
    *,
    current_epoch: int = 0,
) -> str | None:
    """Return the session user, or None if absent/expired/tampered/wrong-key/revoked.

    A cookie whose embedded epoch is below ``current_epoch`` was issued before a
    revocation bump and is rejected. Cookies predating the epoch feature (no
    ``e`` field) read as epoch 0, so they stay valid until the first bump.
    """
    if not token:
        return None
    try:
        data = serializer.loads(token, max_age=max_age)
    except BadSignature:  # covers SignatureExpired (subclass) too
        return None
    if not isinstance(data, dict):
        return None
    try:
        token_epoch = int(data.get("e", 0))
    except (TypeError, ValueError):
        token_epoch = 0
    if token_epoch < current_epoch:
        return None  # issued before the last revocation
    return data.get("u")


# ----- session epoch (logout revocation) -----------------------------------


def read_epoch(state_dir: Path) -> int:
    """Return the current session epoch; a missing/corrupt file means 0 (no revocation yet).

    Never raises — a fresh deploy simply starts at epoch 0.
    """
    path = state_dir.expanduser() / "session.epoch"
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return 0


def bump_epoch(state_dir: Path, floor: int = 0) -> int:
    """Atomically increment + persist the session epoch; return the new value.

    Bumping invalidates every previously-issued cookie (their embedded epoch is
    now stale), so logout becomes a genuine "log out everywhere" revocation that
    a captured cookie can't survive. Atomic write mirrors ``state.StateStore``.

    The new value is ``max(on-disk epoch, floor) + 1``. ``floor`` is the caller's
    last-known epoch — the in-memory ``app.state.session_epoch``, authoritative
    for the running process. Flooring against it means a transient read error or
    a corrupt on-disk value can never *lower* the epoch (which would un-revoke
    previously-revoked cookies), so the read can stay lenient and logout never
    has to fail to preserve the revocation guarantee. A missing/empty file reads
    as 0; the floor carries the real value.
    """
    state_dir = state_dir.expanduser()
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = state_dir / "session.epoch"
    try:
        text = path.read_text(encoding="utf-8").strip()
        disk = int(text) if text else 0
    except (FileNotFoundError, OSError, ValueError):
        disk = 0  # unreadable/corrupt: rely on the floor, never regress
    new_value = max(disk, floor) + 1
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(str(new_value), encoding="utf-8")
    os.replace(tmp, path)
    return new_value


# ----- reverse-proxy HMAC (D13) -------------------------------------------


def verify_proxy_hmac(
    secret: str | None,
    header_value: str | None,
    remote_user: str | None,
    method: str,
    path: str,
    window: int,
    *,
    now: int | None = None,
) -> bool:
    """Verify ``X-Proxy-Auth: t=<unix>,v1=<hex>``.

    The signature commits to ``user:t:method:path`` so a captured token can't be
    replayed against a different endpoint, and is only valid within ``window``
    seconds. Returns False on any malformation rather than raising.
    """
    if not secret or not header_value or not remote_user:
        return False
    parts = dict(p.split("=", 1) for p in header_value.split(",") if "=" in p)
    t_raw, sig = parts.get("t"), parts.get("v1")
    if t_raw is None or sig is None:
        return False
    try:
        t = int(t_raw)
    except ValueError:
        return False
    current = int(time.time()) if now is None else now
    if abs(current - t) > window:
        return False
    msg = f"{remote_user}:{t}:{method}:{path}".encode()
    expected = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


# ----- origins (CSRF + WS, D12) -------------------------------------------


def normalize_origin(origin: str) -> str:
    """scheme://host[:port] lowercased, default port elided, no trailing slash.

    IPv6 hosts are re-bracketed: ``urlsplit`` strips the brackets from
    ``http://[::1]`` (``.hostname`` -> ``::1``), so without this an IPv6 loopback
    origin would normalize to the malformed ``http://::1`` and never match the
    allowlist.
    """
    cleaned = origin.strip().rstrip("/")
    parts = urlsplit(cleaned)
    if not parts.scheme or not parts.hostname:
        return cleaned.lower()
    scheme, host = parts.scheme.lower(), parts.hostname.lower()
    if ":" in host:  # IPv6 literal — urlsplit dropped the surrounding brackets
        host = f"[{host}]"
    default = {"http": 80, "https": 443}.get(scheme)
    port = parts.port
    if port is None or port == default:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def build_allowed_origins(config: ClausterConfig) -> set[str]:
    """Origins permitted for unsafe-method CSRF checks and WS handshakes.

    Loopback binds auto-allow 127.0.0.1/localhost; a non-loopback bind (incl.
    0.0.0.0) auto-allows nothing — the operator must list the real public
    origin(s) in ``auth.allowed_origins`` (a bind host of 0.0.0.0 tells us
    nothing about the browser-facing origin).
    """
    origins: set[str] = set()
    if config.host in _LOOPBACK_HOSTS:
        # Include the IPv6 loopback literal — a browser hitting http://[::1]:port
        # sends that as its Origin, and without it the CSRF/WS check would reject a
        # legitimate loopback request.
        for h in ("127.0.0.1", "localhost", "[::1]"):
            origins.add(normalize_origin(f"http://{h}:{config.port}"))
            origins.add(normalize_origin(f"https://{h}:{config.port}"))
    for extra in config.auth.allowed_origins:
        origins.add(normalize_origin(extra))
    return origins


# ----- peer IP (reverse-proxy allowlist) ----------------------------------


def peer_ip(request) -> str | None:
    """Return the socket peer IP (duck-typed Starlette Request; no import needed).

    A seam so tests can monkeypatch the trusted-peer decision. Never derived
    from X-Forwarded-For — uvicorn is pinned with proxy_headers=False so
    ``request.client.host`` is always the real peer.
    """
    client = getattr(request, "client", None)
    return client.host if client else None


def peer_trusted(ip: str | None, trusted_ips: list[str]) -> bool:
    """Whether ``ip`` falls within any ``trusted_ips`` entry (IP or CIDR)."""
    if not ip or not trusted_ips:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped  # normalize ::ffff:127.0.0.1 -> 127.0.0.1
    for entry in trusted_ips:
        try:
            net = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if addr.version == net.version and addr in net:
            return True
    return False
