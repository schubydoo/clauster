"""Native-HTTPS (`tls`) config block — parsing + fail-closed cert/key validation.

Covers both ``provision = off`` (bring-your-own cert, the original behaviour) and
``provision = self-signed`` config-parsing / fail-closed checks.  The provisioner's
actual cert generation is tested in ``test_tls_provision.py``.

The validation is filesystem-only (existence + readability + absolute resolution)
for ``provision = off``, so the throwaway "cert"/"key" files here are plain text —
no real TLS material is needed and uvicorn is never invoked.  HOME is isolated by
the autouse conftest fixture; no real cert is touched.
"""

from __future__ import annotations

import os

import pytest

from clauster.config import ClausterConfig, load_config, resolve_cert_path


def _cert_key(tmp_path):
    """Create throwaway cert + key files and return their paths."""
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("CERT", encoding="utf-8")
    key.write_text("KEY", encoding="utf-8")
    return cert, key


# ----- absent (the default) ---------------------------------------------


def test_tls_absent_is_off_by_default(write_config):
    config = load_config(write_config())
    assert config.tls is None
    assert config.tls_active is False


# ----- provision = off (bring-your-own cert) — happy path ---------------


def test_tls_parses_and_resolves_absolute(write_config, tmp_path):
    cert, key = _cert_key(tmp_path)
    extra = f"tls:\n  cert_file: {cert}\n  key_file: {key}\n"
    config = load_config(write_config(extra))
    assert config.tls_active is True
    assert config.tls.provision == "off"
    # Stored back as resolved absolute paths (what the server hands uvicorn).
    assert os.path.isabs(config.tls.cert_file)
    assert os.path.isabs(config.tls.key_file)
    assert config.tls.cert_file == str(cert.resolve())
    assert config.tls.key_file == str(key.resolve())


def test_tls_expands_user_and_collapses_traversal(tmp_path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text("CERT", encoding="utf-8")
    key.write_text("KEY", encoding="utf-8")
    # A `..` traversal must resolve to the same canonical absolute file (no `..` left).
    traversal = str(tmp_path / "sub" / ".." / "cert.pem")
    (tmp_path / "sub").mkdir()
    config = ClausterConfig(
        projects_root=str(tmp_path),
        tls={"cert_file": traversal, "key_file": str(key)},
    )
    assert ".." not in config.tls.cert_file
    assert config.tls.cert_file == str(cert.resolve())


# ----- provision = off — fail closed ------------------------------------


def test_tls_missing_cert_fails_closed(write_config, tmp_path):
    _, key = _cert_key(tmp_path)
    missing = tmp_path / "nope.pem"
    extra = f"tls:\n  cert_file: {missing}\n  key_file: {key}\n"
    with pytest.raises(ValueError, match=r"tls\.cert_file does not exist"):
        load_config(write_config(extra))


def test_tls_missing_key_fails_closed(write_config, tmp_path):
    cert, _ = _cert_key(tmp_path)
    missing = tmp_path / "nope.pem"
    extra = f"tls:\n  cert_file: {cert}\n  key_file: {missing}\n"
    with pytest.raises(ValueError, match=r"tls\.key_file does not exist"):
        load_config(write_config(extra))


def test_tls_cert_is_a_directory_fails_closed(write_config, tmp_path):
    _, key = _cert_key(tmp_path)
    a_dir = tmp_path / "dir"
    a_dir.mkdir()
    extra = f"tls:\n  cert_file: {a_dir}\n  key_file: {key}\n"
    with pytest.raises(ValueError, match=r"tls\.cert_file is not a regular file"):
        load_config(write_config(extra))


def test_tls_unreadable_cert_fails_closed(write_config, tmp_path, monkeypatch):
    cert, key = _cert_key(tmp_path)
    extra = f"tls:\n  cert_file: {cert}\n  key_file: {key}\n"
    # Force the not-readable branch without real chmod (the file exists but R_OK fails).
    monkeypatch.setattr("clauster.config.os.access", lambda *a, **k: False)
    with pytest.raises(ValueError, match=r"tls\.cert_file is not readable"):
        load_config(write_config(extra))


def test_tls_requires_cert_when_provision_off(write_config, tmp_path):
    _, key = _cert_key(tmp_path)
    # cert_file omitted with provision=off: validator requires it.
    extra = f"tls:\n  key_file: {key}\n"
    with pytest.raises(ValueError, match="cert_file is required"):
        load_config(write_config(extra))


def test_tls_requires_key_when_provision_off(write_config, tmp_path):
    cert, _ = _cert_key(tmp_path)
    # key_file omitted with provision=off: validator requires it.
    extra = f"tls:\n  cert_file: {cert}\n"
    with pytest.raises(ValueError, match="key_file is required"):
        load_config(write_config(extra))


# ----- provision = self-signed — config parsing -------------------------


def test_tls_provision_self_signed_parses(write_config):
    extra = "tls:\n  provision: self-signed\n  hostnames: [localhost]\n"
    config = load_config(write_config(extra))
    assert config.tls_active is True
    assert config.tls.provision == "self-signed"
    assert config.tls.hostnames == ["localhost"]
    # cert/key paths are None — provisioner fills them at server start.
    assert config.tls.cert_file is None
    assert config.tls.key_file is None


def test_tls_provision_self_signed_multiple_hostnames(write_config):
    extra = "tls:\n  provision: self-signed\n  hostnames: [myhost.local, 192.168.1.1]\n"
    config = load_config(write_config(extra))
    assert config.tls.hostnames == ["myhost.local", "192.168.1.1"]


def test_tls_provision_self_signed_requires_hostnames(write_config):
    extra = "tls:\n  provision: self-signed\n"
    with pytest.raises(ValueError, match="tls.hostnames must list at least one"):
        load_config(write_config(extra))


def test_tls_provision_self_signed_rejects_cert_file(write_config, tmp_path):
    cert, _ = _cert_key(tmp_path)
    extra = f"tls:\n  provision: self-signed\n  hostnames: [localhost]\n  cert_file: {cert}\n"
    with pytest.raises(ValueError, match="must not be set when tls.provision = self-signed"):
        load_config(write_config(extra))


def test_tls_provision_self_signed_rejects_key_file(write_config, tmp_path):
    _, key = _cert_key(tmp_path)
    extra = f"tls:\n  provision: self-signed\n  hostnames: [localhost]\n  key_file: {key}\n"
    with pytest.raises(ValueError, match="must not be set when tls.provision = self-signed"):
        load_config(write_config(extra))


def test_tls_provision_unknown_value_fails(write_config):
    extra = "tls:\n  provision: acme\n  hostnames: [localhost]\n"
    with pytest.raises(ValueError):
        load_config(write_config(extra))


def test_tls_provision_self_signed_rejects_blank_hostname(write_config):
    # Defect 3: a whitespace-only hostname must be rejected (not become a blank SAN).
    extra = 'tls:\n  provision: self-signed\n  hostnames: ["good", "  "]\n'
    with pytest.raises(ValueError, match="blank / whitespace-only"):
        load_config(write_config(extra))


def test_tls_provision_self_signed_strips_hostnames(write_config):
    # Surrounding whitespace is stripped so the SAN is clean.
    extra = 'tls:\n  provision: self-signed\n  hostnames: ["  myhost  "]\n'
    config = load_config(write_config(extra))
    assert config.tls.hostnames == ["myhost"]


# ----- env overrides ----------------------------------------------------


def test_tls_paths_settable_via_env(write_config, tmp_path, monkeypatch):
    cert, key = _cert_key(tmp_path)
    # The optional nested `tls` section's leaves still get CLAUSTER_TLS_<LEAF> env vars.
    monkeypatch.setenv("CLAUSTER_TLS_CERT_FILE", str(cert))
    monkeypatch.setenv("CLAUSTER_TLS_KEY_FILE", str(key))
    config = load_config(write_config())
    assert config.tls_active is True
    assert config.tls.cert_file == str(cert.resolve())


def test_tls_provision_settable_via_env(write_config, monkeypatch):
    monkeypatch.setenv("CLAUSTER_TLS_PROVISION", "self-signed")
    # Use a YAML snippet that already sets provision+hostnames; the env var overrides provision.
    yaml_extra = "tls:\n  provision: self-signed\n  hostnames: [localhost]\n"
    config = load_config(write_config(yaml_extra))
    assert config.tls.provision == "self-signed"


def test_env_map_does_not_recurse_into_projects_dict():
    # Regression: the Optional[BaseModel] env-recursion must NOT also unwrap the
    # `projects: dict[str, ProjectConfig]` map, or it would invent a phantom
    # CLAUSTER_PROJECTS_ALLOW_BYPASS_PERMISSIONS env var that pollutes a security-
    # sensitive section. dict/list leaves stay unmappable, as before the tls change.
    from clauster.config import _env_leaf_map

    env_map = _env_leaf_map(ClausterConfig)
    paths = [path for path, _kind in env_map.values()]
    assert ("projects", "allow_bypass_permissions") not in paths
    assert "CLAUSTER_PROJECTS_ALLOW_BYPASS_PERMISSIONS" not in env_map
    # The intended TLS keys are still there; the bogus whole-section scalar is not.
    assert "CLAUSTER_TLS_CERT_FILE" in env_map
    assert "CLAUSTER_TLS_KEY_FILE" in env_map
    assert "CLAUSTER_TLS_PROVISION" in env_map
    assert "CLAUSTER_TLS" not in env_map


# ----- the shared resolver ----------------------------------------------


def test_resolve_cert_path_message_carries_path_not_bytes(tmp_path):
    # The error names the filesystem path (operator-actionable), never the key bytes.
    missing = tmp_path / "secret-material.pem"
    with pytest.raises(ValueError) as ei:
        resolve_cert_path("key_file", str(missing))
    msg = str(ei.value)
    assert str(missing.resolve()) in msg
    assert "tls.key_file" in msg
