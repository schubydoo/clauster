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


def test_non_loopback_host_rejected(write_config):
    cfg_path = write_config("host: 0.0.0.0\n")
    with pytest.raises(ValueError, match="loopback only"):
        load_config(cfg_path)


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


def test_missing_config_file_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUSTER_CONFIG", raising=False)
    monkeypatch.delenv("CLAUSTER_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        load_config()
