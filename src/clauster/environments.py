"""v0.3 ghost-environment reaper (spec §11 environments-management API).

Lists / archives the server-side bridge environments (``env_<ULID>``) that no
longer back a live bridge — the ghosts that clutter the claude.ai/code "New
session" selector after a bridge dies or a host restarts.

Safety, by construction (see memory `reference-clauster-env-api`):
  - credentials come from ONE documented file each (never a multi-location scan)
    and the token is masked in any log/repr;
  - a *cloud* environment (``config.type == "cloud"``, the "Default") is NEVER a
    reap candidate;
  - an env is a ghost only when it is a *bridge* whose ``config.directory`` has no
    live bridge on this host — an unknown/absent directory is left alone;
  - all HTTP goes through an injectable transport, so the logic is fully testable
    offline; archive (reversible) is preferred and force-delete is a separate gate.
"""

from __future__ import annotations

import http.client
import json
import logging
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

_log = logging.getLogger("clauster.environments")

# Hard ceiling on pagination so a misbehaving endpoint can't spin forever; at the
# default page size this is ~1M environments, far beyond any real account.
_MAX_LIST_PAGES = 10_000

API_BASE = "https://api.anthropic.com"
# Date-stamped and the CLI moves fast — re-verify against the installed `claude`
# binary (grep it for `managed-agents-`/`environments-`) before trusting blindly.
BETA_HEADER = "managed-agents-2026-04-01"

CREDENTIALS_PATH = Path("~/.claude/.credentials.json")
CLAUDE_JSON_PATH = Path("~/.claude.json")

# A transport maps (method, url, headers, body) -> (status, raw_bytes). The default
# (``_https_transport``) uses http.client.HTTPSConnection; tests inject a fake so
# nothing hits the network.
Transport = Callable[[str, str, dict, bytes | None], "tuple[int, bytes]"]


class CredentialsError(RuntimeError):
    """A required credential file/field is missing, malformed, or expired."""


class EnvironmentsAPIError(RuntimeError):
    """The environments API returned a non-2xx status or an unusable body."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"environments API returned {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass
class Credentials:
    """OAuth access token + org UUID used to call the environments API."""

    access_token: str
    organization_uuid: str
    expires_at: int | None = None  # ms epoch, if present

    def masked_token(self) -> str:
        """Return the token's first 6 chars plus an ellipsis (safe for logs)."""
        return (self.access_token[:6] + "…") if self.access_token else "(none)"


def load_credentials(
    credentials_path: Path = CREDENTIALS_PATH,
    claude_json_path: Path = CLAUDE_JSON_PATH,
    *,
    now_ms: int | None = None,
) -> Credentials:
    """Read the OAuth access token + org UUID from their single documented files.

    Parsed with the stdlib (do NOT assume ``jq`` is installed). Raises
    ``CredentialsError`` on a missing file/field or an expired token.
    """
    cred_file = Path(credentials_path).expanduser()
    try:
        oauth = json.loads(cred_file.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
    except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        raise CredentialsError(f"could not read {cred_file}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CredentialsError(f"{cred_file} is not valid JSON: {exc}") from exc

    token = oauth.get("accessToken")
    if not token:
        raise CredentialsError(f"no claudeAiOauth.accessToken in {cred_file}")
    expires_at = oauth.get("expiresAt")
    if expires_at is not None and now_ms is not None and now_ms >= expires_at:
        raise CredentialsError("access token has expired; re-authenticate with `claude`")

    json_file = Path(claude_json_path).expanduser()
    try:
        org = (json.loads(json_file.read_text(encoding="utf-8")).get("oauthAccount") or {}).get(
            "organizationUuid"
        )
    except (FileNotFoundError, OSError, UnicodeDecodeError) as exc:
        raise CredentialsError(f"could not read {json_file}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CredentialsError(f"{json_file} is not valid JSON: {exc}") from exc
    if not org:
        raise CredentialsError(f"no oauthAccount.organizationUuid in {json_file}")

    return Credentials(access_token=token, organization_uuid=org, expires_at=expires_at)


class EnvironmentConfig(BaseModel):
    """The ``config`` block of an environment (type + directory; extras allowed)."""

    type: str = ""
    directory: str | None = None
    model_config = {"extra": "allow"}


class Environment(BaseModel):
    """One item from ``GET /v1/environments`` (Anthropic-controlled shape)."""

    id: str
    name: str = ""
    config: EnvironmentConfig = Field(default_factory=EnvironmentConfig)
    model_config = {"extra": "allow"}

    @property
    def is_cloud(self) -> bool:
        """Whether this is a cloud environment (never reaped)."""
        return self.config.type == "cloud"

    @property
    def is_bridge(self) -> bool:
        """Whether this is a bridge environment (the reaper's candidate type)."""
        return self.config.type == "bridge"


def _https_transport(
    method: str, url: str, headers: dict, body: bytes | None
) -> tuple[int, bytes]:
    # http.client.HTTPSConnection is HTTPS-only by construction (no file:// / other
    # schemes possible, unlike urllib.urlopen), so a malformed base/path can't be
    # coerced into reading a local file. The scheme guard is unit-tested; the actual
    # network round-trip below is the live I/O boundary (exercised against the real
    # API, not in the offline suite).
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise EnvironmentsAPIError(0, f"refusing non-https URL: {url!r}")
    return _https_roundtrip(method, parts, headers, body)


def _https_roundtrip(method, parts, headers, body):  # pragma: no cover - live network I/O
    # Explicit verifying context checks cert chain + hostname (rule is a cross-version
    # audit nag; on py3.11+ with create_default_context() certs ARE verified) — never disable.
    # nosemgrep: python.lang.security.audit.httpsconnection-detected.httpsconnection-detected
    conn = http.client.HTTPSConnection(
        parts.netloc, timeout=30, context=ssl.create_default_context()
    )
    try:
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


class EnvironmentsClient:
    """Minimal client for the Anthropic environments API (list/archive/delete)."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        transport: Transport | None = None,
        base: str = API_BASE,
    ) -> None:
        self._cred = credentials
        self._transport = transport or _https_transport
        self._base = base.rstrip("/")

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._cred.access_token}",
            "x-organization-uuid": self._cred.organization_uuid,
            "anthropic-beta": BETA_HEADER,
            "content-type": "application/json",
        }

    def _request(self, method: str, path: str) -> dict:
        status, raw = self._transport(method, self._base + path, self._headers(), None)
        if status >= 400:
            raise EnvironmentsAPIError(status, raw.decode("utf-8", "replace")[:500])
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # A 2xx with a non-JSON body (proxy error page, truncated response) must
            # not crash the reaper with a bare decode error — surface it as an API error.
            raise EnvironmentsAPIError(status, f"non-JSON response body: {exc}") from exc

    def list_environments(self, *, limit: int = 100) -> list[Environment]:
        """All environments, following ``next_page`` (after_id) pagination.

        Bounded against a misbehaving endpoint: stops if a cursor repeats (a cycle)
        or the page count exceeds ``_MAX_LIST_PAGES``, returning what was collected.
        """
        out: list[Environment] = []
        after: str | None = None
        seen: set[str] = set()
        for _ in range(_MAX_LIST_PAGES):
            path = f"/v1/environments?limit={limit}"
            if after:
                path += f"&after_id={after}"
            data = self._request("GET", path)
            out.extend(Environment.model_validate(e) for e in data.get("data", []))
            after = data.get("next_page")
            if not after or after in seen:
                return out
            seen.add(after)
        _log.warning(
            "environments pagination hit the %d-page ceiling; returning a partial list",
            _MAX_LIST_PAGES,
        )
        return out

    def archive_environment(self, env_id: str) -> None:
        """Reversible: makes the env read-only and drains its queue (preferred over delete)."""
        self._request("POST", f"/v1/environments/{env_id}/archive")

    def delete_environment(self, env_id: str, *, force: bool = False) -> None:
        """Hard delete. 409 if work is queued unless ``force`` (which discards it)."""
        path = f"/v1/environments/{env_id}"
        if force:
            path += "?force=true"
        self._request("DELETE", path)


def find_ghosts(environments: list[Environment], live_directories: set[str]) -> list[Environment]:
    """Bridge environments whose directory has no live bridge on this host.

    NEVER includes a cloud environment. A bridge env with an unknown/absent
    ``directory`` is conservatively skipped (we don't reap what we can't attribute).
    """
    live = {_norm(d) for d in live_directories}
    ghosts: list[Environment] = []
    for env in environments:
        if env.is_cloud or not env.is_bridge:
            continue
        directory = env.config.directory
        if not directory:
            continue  # unattributable -> leave it alone
        if _norm(directory) not in live:
            ghosts.append(env)
    return ghosts


def _norm(directory: str) -> str:
    return str(Path(directory).expanduser().resolve())


def live_bridge_directories(binary: str, projects_root: Path | None = None) -> set[str]:
    """Directories that currently host a live bridge (the reaper's "keep" set).

    Sourced from ``claude agents --json`` cwds (host-wide live sessions) plus a
    live-pointer walk under projects_root. **Deliberately NOT best-effort**: if the
    agents-json probe fails this raises, because reaping with an incomplete live set
    could archive a still-live bridge. The CLI must abort rather than guess.
    """
    from . import inspector, pointers
    from .discovery import discover_projects

    dirs = {str(s.cwd) for s in inspector.list_working_sessions(binary)}
    if projects_root is not None:
        for proj in discover_projects(projects_root):
            ptr = pointers.pointer_for_project(proj.path)
            if ptr is not None and pointers.is_live(ptr):
                dirs.add(str(proj.path))
    return dirs
