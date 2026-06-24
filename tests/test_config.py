from __future__ import annotations

import logging

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


@pytest.mark.parametrize(
    "extra",
    [
        "port: 0\n",
        "port: 70000\n",
        "instance_defaults:\n  capacity: 0\n",
        "clone:\n  timeout_seconds: 0\n",
        "logs:\n  bridge_log_max_size_mb: 0\n",
    ],
)
def test_out_of_range_numeric_config_rejected(write_config, extra):
    with pytest.raises(ValueError):
        load_config(write_config(extra))


def test_malformed_clone_cidr_rejected(write_config):
    with pytest.raises(ValueError):
        load_config(write_config('clone:\n  allowed_private_cidrs: ["not-a-cidr"]\n'))


def test_valid_clone_cidr_accepted(write_config):
    config = load_config(
        write_config('clone:\n  allowed_private_cidrs: ["192.168.0.0/16", "10.0.0.0/8"]\n')
    )
    assert config.clone.allowed_private_cidrs == ["192.168.0.0/16", "10.0.0.0/8"]


def test_env_config_and_home_candidates(write_config, monkeypatch):
    # Setting CLAUSTER_CONFIG / CLAUSTER_HOME exercises both candidate-path branches.
    cfg_path = write_config()
    monkeypatch.setenv("CLAUSTER_CONFIG", str(cfg_path))
    monkeypatch.setenv("CLAUSTER_HOME", str(cfg_path.parent))
    config = load_config()  # no explicit path -> resolves via env
    assert config.source_path == cfg_path


def test_env_file_indirection_reads_secret(write_config, monkeypatch, tmp_path):
    # CLAUSTER_<X>_FILE reads the value from a file (trailing newline stripped) — for
    # secrets rendered to /run/secrets by Docker/K8s/Vault (#368).
    secret_file = tmp_path / "pw_hash"
    secret_file.write_text("argon2-hash-from-file\n", encoding="utf-8")
    monkeypatch.setenv("CLAUSTER_AUTH_PASSWORD_HASH_FILE", str(secret_file))
    config = load_config(write_config())
    assert config.auth.password_hash == "argon2-hash-from-file"


def test_env_file_wins_over_plain_var(write_config, monkeypatch, tmp_path):
    secret_file = tmp_path / "pw_hash"
    secret_file.write_text("from-file", encoding="utf-8")
    monkeypatch.setenv("CLAUSTER_AUTH_PASSWORD_HASH", "from-env")
    monkeypatch.setenv("CLAUSTER_AUTH_PASSWORD_HASH_FILE", str(secret_file))
    config = load_config(write_config())
    assert config.auth.password_hash == "from-file"


def test_blank_env_file_falls_through_to_plain_var(write_config, monkeypatch):
    # A blank _FILE is treated as unset so the plain env var still applies.
    monkeypatch.setenv("CLAUSTER_AUTH_PASSWORD_HASH_FILE", "   ")
    monkeypatch.setenv("CLAUSTER_AUTH_PASSWORD_HASH", "from-env")
    config = load_config(write_config())
    assert config.auth.password_hash == "from-env"


def test_unreadable_env_file_fails_closed(write_config, monkeypatch, tmp_path):
    # A _FILE pointing at a missing file is a misconfiguration — fail closed, never
    # silently fall back to the (possibly empty) plain env var.
    monkeypatch.setenv("CLAUSTER_AUTH_PASSWORD_HASH_FILE", str(tmp_path / "nope"))
    with pytest.raises(ValueError, match="unreadable file"):
        load_config(write_config())


def test_empty_env_file_fails_closed(write_config, monkeypatch, tmp_path):
    # A present-but-empty (or whitespace-only) secret file is a blank-rendered secret —
    # raise rather than apply "" (which would look like an absent override).
    secret_file = tmp_path / "pw_hash"
    secret_file.write_text("   \n", encoding="utf-8")
    monkeypatch.setenv("CLAUSTER_AUTH_PASSWORD_HASH_FILE", str(secret_file))
    with pytest.raises(ValueError, match="empty file"):
        load_config(write_config())


def test_non_utf8_env_file_fails_closed(write_config, monkeypatch, tmp_path):
    # A binary/corrupt mount must fail closed with the documented error, and the
    # offending bytes must NOT appear in the message (no secret-fragment leak).
    secret_file = tmp_path / "pw_hash"
    secret_file.write_bytes(b"\xff\xfe\x80secret")
    monkeypatch.setenv("CLAUSTER_AUTH_PASSWORD_HASH_FILE", str(secret_file))
    with pytest.raises(ValueError, match="not valid UTF-8") as exc:
        load_config(write_config())
    assert "secret" not in str(exc.value)


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
        # reverse-proxy auth only counts when auth.enabled is set (it's the runtime gate).
        "host: 0.0.0.0\nauth:\n  enabled: true\n  reverse_proxy:\n    enabled: true\n",
        "host: 0.0.0.0\nauth:\n  enabled: true\n  password_required: true\n"
        f"  password_hash: '{_HASH}'\n",
        "host: 0.0.0.0\nauth:\n  allow_unauthenticated_network: true\n",
    ],
)
def test_non_loopback_allowed_with_auth(write_config, extra):
    config = load_config(write_config(extra))
    assert config.host == "0.0.0.0"


@pytest.mark.parametrize(
    "extra",
    [
        # The footgun: a password (or proxy) is configured but auth.enabled is left at its
        # false default, so the runtime guard would serve the dashboard unauthenticated.
        # The validator must refuse rather than start a silently-open non-loopback bind.
        f"host: 0.0.0.0\nauth:\n  password_required: true\n  password_hash: '{_HASH}'\n",
        "host: 0.0.0.0\nauth:\n  reverse_proxy:\n    enabled: true\n",
    ],
)
def test_non_loopback_rejected_when_auth_not_enabled(write_config, extra):
    with pytest.raises(ValueError, match="without enforced auth"):
        load_config(write_config(extra))


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


# A syntactically valid hash: 64 lowercase hex chars (deadbeef * 8).
_VALID_TOKEN_HASH = "deadbeef" * 8


def test_api_token_counts_as_enforced_auth(write_config):
    # An api_token_hash (with auth.enabled) is a real enforced-auth method, so a
    # non-loopback bind is permitted without a password or reverse proxy (#360).
    config = load_config(
        write_config(
            f"host: 0.0.0.0\nauth:\n  enabled: true\n  api_token_hash: '{_VALID_TOKEN_HASH}'\n",
        )
    )
    assert config.host == "0.0.0.0"
    assert config.auth.api_token_hash == _VALID_TOKEN_HASH


def test_api_token_without_enabled_rejected(write_config):
    # Same footgun guard as password/proxy: a token configured but auth.enabled
    # left false is a silent open door on a non-loopback bind -> refuse to start.
    with pytest.raises(ValueError, match="without enforced auth"):
        load_config(
            write_config(f"host: 0.0.0.0\nauth:\n  api_token_hash: '{_VALID_TOKEN_HASH}'\n")
        )


@pytest.mark.parametrize("bad", ["not_a_real_hash", "deadbeef", "DEADBEEF" * 8, "g" * 64])
def test_malformed_api_token_hash_rejected(write_config, bad):
    # A non-empty value that is not a 64-char lowercase hex digest can never match a
    # token yet would otherwise satisfy the enforced-auth check and permit a
    # non-loopback bind -- a dashboard no token can ever unlock. Reject it loudly.
    with pytest.raises(ValueError, match="64-character lowercase hex"):
        load_config(write_config(f"auth:\n  enabled: true\n  api_token_hash: '{bad}'\n"))


@pytest.mark.parametrize("blank", ["''", "'   '", '"\t"'])
def test_blank_api_token_hash_is_not_enforced_auth(write_config, blank):
    # A blank/whitespace-only hash can never authenticate a token, so it must NOT
    # count as enforced auth — a non-loopback bind that relies on it alone is the
    # same GHSA-#88 open-door footgun and must be refused (the validator maps the
    # blank value to None so it is treated as unset, like an empty password_hash).
    with pytest.raises(ValueError, match="without enforced auth"):
        load_config(
            write_config(f"host: 0.0.0.0\nauth:\n  enabled: true\n  api_token_hash: {blank}\n")
        )


def test_blank_api_token_hash_normalizes_to_none(write_config):
    # Loopback bind so the config is otherwise valid: the blank hash is normalized
    # to None rather than retained as a falsely-"configured" credential.
    config = load_config(write_config("auth:\n  enabled: true\n  api_token_hash: '   '\n"))
    assert config.auth.api_token_hash is None


@pytest.mark.parametrize("bad", ["not_a_real_hash", "deadbeef", "DEADBEEF" * 8, "g" * 64])
def test_malformed_metrics_token_hash_rejected(write_config, bad):
    # Parity with api_token_hash (#473): the metrics scrape token is stored as a
    # SHA-256 hash, so a non-empty value that is not a 64-char lowercase hex digest
    # can never match a presented token. Reject it loudly so the operator fixes it.
    with pytest.raises(ValueError, match="64-character lowercase hex"):
        load_config(write_config(f"observability:\n  metrics_token_hash: '{bad}'\n"))


@pytest.mark.parametrize("blank", ["''", "'   '", '"\t"'])
def test_blank_metrics_token_hash_normalizes_to_none(write_config, blank):
    # An empty / whitespace-only hash stays unset (None): /metrics simply remains
    # behind the auth guard, no token path.
    config = load_config(write_config(f"observability:\n  metrics_token_hash: {blank}\n"))
    assert config.observability.metrics_token_hash is None


def test_valid_metrics_token_hash_accepted(write_config):
    # A 64-char lowercase hex digest loads unchanged.
    config = load_config(
        write_config(f"observability:\n  metrics_token_hash: '{_VALID_TOKEN_HASH}'\n")
    )
    assert config.observability.metrics_token_hash == _VALID_TOKEN_HASH


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


def test_env_override_merges_into_existing_nested_mapping(write_config, monkeypatch):
    # When the yaml already defines the parent mapping (`logs`), a nested env
    # override must merge into the existing dict rather than replacing it — i.e.
    # _set_nested reuses the present sub-dict (the keep-existing branch) so the
    # config-file sibling key survives alongside the env-set one.
    cfg_path = write_config("logs:\n  keep_rotated: 9\n")
    monkeypatch.setenv("CLAUSTER_LOGS_STRIP_ANSI_IN_STREAM", "false")
    config = load_config(cfg_path)
    assert config.logs.strip_ansi_in_stream is False  # env override applied
    assert config.logs.keep_rotated == 9  # the file-set sibling key was preserved


def test_reaper_ui_disabled_by_default(write_config):
    assert load_config(write_config()).reaper.ui_enabled is False


def test_reaper_ui_enabled_via_config(write_config):
    config = load_config(write_config("reaper:\n  ui_enabled: true\n"))
    assert config.reaper.ui_enabled is True


def test_reaper_ui_env_override(write_config, monkeypatch):
    cfg_path = write_config()
    monkeypatch.setenv("CLAUSTER_REAPER_UI_ENABLED", "true")
    assert load_config(cfg_path).reaper.ui_enabled is True


def test_observability_prometheus_disabled_by_default(write_config):
    assert load_config(write_config()).observability.prometheus_enabled is False


def test_observability_prometheus_enabled_via_config(write_config):
    config = load_config(write_config("observability:\n  prometheus_enabled: true\n"))
    assert config.observability.prometheus_enabled is True


def test_observability_prometheus_env_override(write_config, monkeypatch):
    cfg_path = write_config()
    monkeypatch.setenv("CLAUSTER_OBSERVABILITY_PROMETHEUS_ENABLED", "true")
    assert load_config(cfg_path).observability.prometheus_enabled is True


def test_usage_mode_default_cost(write_config):
    u = load_config(write_config()).usage
    assert u.mode == "cost"
    assert u.show_cost is True  # deprecated alias still present, default on


def test_usage_mode_tokens_via_config(write_config):
    assert load_config(write_config("usage:\n  mode: tokens\n")).usage.mode == "tokens"


def test_usage_mode_off_via_config(write_config):
    assert load_config(write_config("usage:\n  mode: off\n")).usage.mode == "off"


def test_usage_show_cost_false_resolves_to_mode_off(write_config):
    # Back-compat: show_cost was the old hide switch -> it must force mode "off".
    config = load_config(write_config("usage:\n  show_cost: false\n"))
    assert config.usage.show_cost is False
    assert config.usage.mode == "off"


def test_usage_show_cost_false_overrides_explicit_mode_with_warning(write_config, caplog):
    with caplog.at_level(logging.WARNING, logger="clauster.config"):
        config = load_config(write_config("usage:\n  mode: tokens\n  show_cost: false\n"))
    assert config.usage.mode == "off"  # show_cost=false wins
    assert any("show_cost=false overrides" in r.message for r in caplog.records)


def test_usage_currency_default_usd(write_config):
    assert load_config(write_config()).usage.currency == "USD"


def test_usage_currency_override_via_config(write_config):
    assert load_config(write_config("usage:\n  currency: EUR\n")).usage.currency == "EUR"


def test_notifications_defaults(write_config):
    n = load_config(write_config()).notifications
    assert n.enabled is False
    assert n.urls == []
    assert n.notify_on_crash is True


def test_notifications_via_config(write_config):
    n = load_config(
        write_config("notifications:\n  enabled: true\n  urls:\n    - 'slack://x'\n")
    ).notifications
    assert n.enabled is True
    assert n.urls == ["slack://x"]


def test_notifications_notify_on_crash_override(write_config):
    extra = (
        "notifications:\n  enabled: true\n  urls:\n    - 'slack://x'\n  notify_on_crash: false\n"
    )
    n = load_config(write_config(extra)).notifications
    assert n.notify_on_crash is False


def test_usage_currency_normalized_to_uppercase(write_config, caplog):
    # A lowercase code must compare equal to USD (no spurious symbol/FX fallback).
    with caplog.at_level(logging.WARNING, logger="clauster.config"):
        u = load_config(write_config("usage:\n  currency: ' usd '\n")).usage
    assert u.currency == "USD"
    assert u.effective_symbol == "$"
    assert not any("fx_rate" in r.message for r in caplog.records)


def test_usage_fx_rate_default_one(write_config):
    assert load_config(write_config()).usage.fx_rate == 1.0


def test_usage_fx_rate_must_be_positive(write_config):
    with pytest.raises(ValueError):
        load_config(write_config("usage:\n  fx_rate: 0\n"))


def test_usage_token_total_includes_cache_default_true(write_config):
    assert load_config(write_config()).usage.token_total_includes_cache is True


def test_usage_effective_symbol_defaults_to_dollar_for_usd(write_config):
    assert load_config(write_config()).usage.effective_symbol == "$"


def test_usage_effective_symbol_explicit_wins(write_config):
    u = load_config(write_config("usage:\n  currency: EUR\n  currency_symbol: '€'\n")).usage
    assert u.effective_symbol == "€"


def test_usage_effective_symbol_non_usd_falls_back_to_code(write_config):
    # No explicit symbol + non-USD currency -> the code is the prefix (not a bare "$").
    u = load_config(write_config("usage:\n  currency: GBP\n  fx_rate: 0.79\n")).usage
    assert u.effective_symbol == "GBP "


def test_usage_foreign_currency_without_fx_rate_warns(write_config, caplog):
    # currency != USD while fx_rate stays 1.0 would paint a foreign label on a USD figure.
    with caplog.at_level(logging.WARNING, logger="clauster.config"):
        load_config(write_config("usage:\n  currency: EUR\n"))
    assert any("fx_rate=1.0" in r.message for r in caplog.records)


def test_usage_foreign_currency_with_fx_rate_does_not_warn(write_config, caplog):
    with caplog.at_level(logging.WARNING, logger="clauster.config"):
        load_config(write_config("usage:\n  currency: EUR\n  fx_rate: 0.92\n"))
    assert not any("fx_rate" in r.message for r in caplog.records)


def test_metrics_defaults(write_config):
    m = load_config(write_config()).metrics
    assert m.enabled is True
    assert m.normalize_cpu is False
    assert m.show_disk is True
    assert m.sample_interval_seconds == 0.15
    assert m.poll_seconds == 4.0


def test_metrics_overrides_via_config(write_config):
    m = load_config(
        write_config(
            "metrics:\n  enabled: false\n  normalize_cpu: true\n  show_disk: false\n"
            "  sample_interval_seconds: 0.5\n  poll_seconds: 10\n"
        )
    ).metrics
    assert m.enabled is False
    assert m.normalize_cpu is True
    assert m.show_disk is False
    assert m.sample_interval_seconds == 0.5
    assert m.poll_seconds == 10.0


def test_metrics_out_of_range_rejected(write_config):
    with pytest.raises(ValueError):
        load_config(write_config("metrics:\n  sample_interval_seconds: 0\n"))
    with pytest.raises(ValueError):
        load_config(write_config("metrics:\n  sample_interval_seconds: 5\n"))  # > 2.0 cap
    with pytest.raises(ValueError):
        load_config(write_config("metrics:\n  poll_seconds: 0.5\n"))  # < 1.0 floor


def test_missing_config_file_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUSTER_CONFIG", raising=False)
    monkeypatch.delenv("CLAUSTER_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(FileNotFoundError):
        load_config()


def test_instance_name_default_and_valid(write_config):
    assert load_config(write_config()).instance_name is None
    assert load_config(write_config("instance_name: dev\n")).instance_name == "dev"


def test_instance_name_invalid_rejected(write_config):
    # constrained charset (no spaces/brackets) + 32-char cap keep it safe + tidy in
    # a process title.
    with pytest.raises(ValueError):
        load_config(write_config("instance_name: 'has spaces'\n"))
    with pytest.raises(ValueError):
        load_config(write_config(f"instance_name: {'x' * 33}\n"))


def test_instance_name_env_override(write_config, monkeypatch):
    # top-level scalar -> CLAUSTER_INSTANCE_NAME works for free (handy for systemd).
    monkeypatch.setenv("CLAUSTER_INSTANCE_NAME", "prod")
    assert load_config(write_config()).instance_name == "prod"


def test_legacy_resume_mode_yaml_key_maps_to_launch_mode(write_config, caplog):
    # #540: `claude.resume_mode` was renamed to `claude.launch_mode`. Old clauster.yml
    # files keep working: the legacy key loads, maps to launch_mode, and warns (not silent).
    import logging

    with caplog.at_level(logging.WARNING, logger="clauster.config"):
        config = load_config(write_config("claude:\n  resume_mode: pty\n"))
    assert config.claude.launch_mode == "pty"
    assert any(
        "resume_mode" in r.message and "launch_mode" in r.message for r in caplog.records
    ), "expected a deprecation warning naming both keys"


def test_launch_mode_wins_when_both_keys_set(write_config, caplog):
    # Both keys set: the new launch_mode wins and the legacy key is ignored (with a warning),
    # rather than silently picking one.
    import logging

    with caplog.at_level(logging.WARNING, logger="clauster.config"):
        config = load_config(
            write_config("claude:\n  resume_mode: pty\n  launch_mode: standard\n")
        )
    assert config.claude.launch_mode == "standard"
    assert any("both" in r.message and "resume_mode" in r.message for r in caplog.records)


def test_legacy_resume_mode_env_var_maps_to_launch_mode(write_config, monkeypatch, caplog):
    # The renamed key never silently loses its env override: the old CLAUSTER_CLAUDE_RESUME_MODE
    # still applies to launch_mode, with a deprecation warning.
    import logging

    monkeypatch.setenv("CLAUSTER_CLAUDE_RESUME_MODE", "pty")
    with caplog.at_level(logging.WARNING, logger="clauster.config"):
        config = load_config(write_config())
    assert config.claude.launch_mode == "pty"
    assert any("CLAUSTER_CLAUDE_RESUME_MODE" in r.message for r in caplog.records)


def test_new_launch_mode_env_var_wins_over_legacy(write_config, monkeypatch, caplog):
    # When both env vars are set, the new name wins AND we warn (mirrors the YAML both-keys
    # path) so a stale legacy env override is never silently ignored.
    import logging

    monkeypatch.setenv("CLAUSTER_CLAUDE_RESUME_MODE", "pty")
    monkeypatch.setenv("CLAUSTER_CLAUDE_LAUNCH_MODE", "standard")
    with caplog.at_level(logging.WARNING, logger="clauster.config"):
        config = load_config(write_config())
    assert config.claude.launch_mode == "standard"
    assert any(
        "both" in r.message and "CLAUSTER_CLAUDE_RESUME_MODE" in r.message for r in caplog.records
    )


def test_legacy_resume_mode_file_env_alias_maps_to_launch_mode(
    write_config, monkeypatch, tmp_path
):
    # The legacy alias also honors the CLAUSTER_<X>_FILE secret-indirection form.
    secret = tmp_path / "mode"
    secret.write_text("pty\n", encoding="utf-8")  # trailing newline is stripped by the reader
    monkeypatch.setenv("CLAUSTER_CLAUDE_RESUME_MODE_FILE", str(secret))
    assert load_config(write_config()).claude.launch_mode == "pty"
