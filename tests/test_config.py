from __future__ import annotations

from pathlib import Path

import pytest

from clauster.config import load_config


def test_loads_minimal_config(write_config, projects_root):
    cfg_path = write_config()
    config = load_config(cfg_path)
    assert config.projects_root == projects_root
    assert config.host == "127.0.0.1"
    assert config.port == 7621
    assert config.claude.binary == "claude"
    assert config.instance_defaults.capacity == 32
    assert config.source_path == cfg_path


def test_missing_projects_root_rejected(tmp_path):
    cfg = tmp_path / "clauster.yml"
    cfg.write_text(f"projects_root: {tmp_path / 'does-not-exist'}\n")
    with pytest.raises(ValueError, match="projects_root does not exist"):
        load_config(cfg)


def test_unreadable_projects_root_rejected(tmp_path, monkeypatch):
    pr = tmp_path / "noread"
    pr.mkdir()
    cfg = tmp_path / "clauster.yml"
    cfg.write_text(f"projects_root: {pr}\n")
    # Force the not-readable branch without real chmod (the dir exists but R_OK fails).
    monkeypatch.setattr("clauster.config.os.access", lambda *a, **k: False)
    with pytest.raises(ValueError, match="not readable"):
        load_config(cfg)


def test_non_mapping_config_root_rejected(tmp_path):
    cfg = tmp_path / "clauster.yml"
    cfg.write_text("- a\n- b\n")  # a YAML list, not a mapping
    with pytest.raises(ValueError, match="must be a mapping"):
        load_config(cfg)


@pytest.mark.parametrize("extra", [
    "port: 0\n",
    "port: 70000\n",
    "instance_defaults:\n  capacity: 0\n",
    "clone:\n  timeout_seconds: 0\n",
    "logs:\n  bridge_log_max_size_mb: 0\n",
])
def test_out_of_range_numeric_config_rejected(write_config, extra):
    with pytest.raises(ValueError):
        load_config(write_config(extra))


def test_malformed_clone_cidr_rejected(write_config):
    with pytest.raises(ValueError):
        load_config(write_config('clone:\n  allowed_private_cidrs: ["not-a-cidr"]\n'))


def test_valid_clone_cidr_accepted(write_config):
    config = load_config(write_config('clone:\n  allowed_private_cidrs: ["192.168.0.0/16", "10.0.0.0/8"]\n'))
    assert config.clone.allowed_private_cidrs == ["192.168.0.0/16", "10.0.0.0/8"]


def test_env_config_and_home_candidates(write_config, monkeypatch):
    # Setting CLAUSTER_CONFIG / CLAUSTER_HOME exercises both candidate-path branches.
    cfg_path = write_config()
    monkeypatch.setenv("CLAUSTER_CONFIG", str(cfg_path))
    monkeypatch.setenv("CLAUSTER_HOME", str(cfg_path.parent))
    config = load_config()  # no explicit path -> resolves via env
    assert config.source_path == cfg_path


def test_non_loopback_host_rejected_without_auth(write_config):
    cfg_path = write_config("host: 0.0.0.0\n")
    with pytest.raises(ValueError, match="non-loopback"):
        load_config(cfg_path)


# A valid argon2id hash for the fixtures (password "hunter2"); see clauster hash-password.
_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "c29tZXNhbHRzb21lc2FsdA$RdjKDIYxJYDjN8a8B0xBkY7Q3oN2pZqkXz0m3l1H4sM"
)


@pytest.mark.parametrize(
    "extra",
    [
        "host: 0.0.0.0\nauth:\n  reverse_proxy:\n    enabled: true\n",
        "host: 0.0.0.0\nauth:\n  allow_unauthenticated_network: true\n",
    ],
)
def test_non_loopback_allowed_with_auth(write_config, extra):
    config = load_config(write_config(extra))
    assert config.host == "0.0.0.0"


def test_password_required_without_hash_fails_closed(write_config):
    cfg_path = write_config("auth:\n  enabled: true\n  password_required: true\n")
    with pytest.raises(ValueError, match="password_hash is empty"):
        load_config(cfg_path)


def test_password_required_with_hash_ok(write_config):
    config = load_config(
        write_config(
            "host: 0.0.0.0\n"
            "auth:\n  enabled: true\n  password_required: true\n"
            f"  password_hash: '{_HASH}'\n"
        )
    )
    assert config.auth.password_required is True
    assert config.host == "0.0.0.0"


def test_env_override_scalar(write_config, monkeypatch):
    cfg_path = write_config()
    monkeypatch.setenv("CLAUSTER_PORT", "9999")
    monkeypatch.setenv("CLAUSTER_CLAUDE_BINARY", "/opt/claude")
    config = load_config(cfg_path)
    assert config.port == 9999
    assert config.claude.binary == "/opt/claude"


def test_env_override_nested_bool(write_config, monkeypatch):
    cfg_path = write_config()
    monkeypatch.setenv("CLAUSTER_LOGS_STRIP_ANSI_IN_STREAM", "false")
    config = load_config(cfg_path)
    assert config.logs.strip_ansi_in_stream is False


def test_reaper_ui_disabled_by_default(write_config):
    assert load_config(write_config()).reaper.ui_enabled is False


def test_reaper_ui_enabled_via_config(write_config):
    config = load_config(write_config("reaper:\n  ui_enabled: true\n"))
    assert config.reaper.ui_enabled is True


def test_reaper_ui_env_override(write_config, monkeypatch):
    cfg_path = write_config()
    monkeypatch.setenv("CLAUSTER_REAPER_UI_ENABLED", "true")
    assert load_config(cfg_path).reaper.ui_enabled is True


def test_missing_config_file_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUSTER_CONFIG", raising=False)
    monkeypatch.delenv("CLAUSTER_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        load_config()
