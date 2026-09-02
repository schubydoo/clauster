from __future__ import annotations

import logging

import pytest

from clauster.config import TooDeeplyNestedYamlError, UnfittingYamlTagError, load_config


def test_loads_minimal_config(write_config, projects_root):
    cfg_path = write_config()
    config = load_config(cfg_path)
    assert config.projects_root == projects_root
    assert config.host == "127.0.0.1"
    assert config.port == 7621
    assert config.instance_defaults.capacity == 32
    assert config.instance_defaults.verbose is False  # off by default
    assert config.source_path == cfg_path


def test_claude_binary_defaults_to_claude():
    # The schema default is the bare name `claude` (resolved to an absolute path
    # before spawning). Asserted against the model default directly rather than the
    # write_config fixture, which points the binary at the fake stub for tests that
    # exercise CLI-invoking endpoints (e.g. /healthz).
    from clauster.config import ClausterConfig

    assert ClausterConfig(projects_root=".").claude.binary == "claude"


def test_instance_defaults_verbose_round_trips(write_config):
    cfg_path = write_config("instance_defaults:\n  verbose: true\n")
    config = load_config(cfg_path)
    assert config.instance_defaults.verbose is True


def test_legacy_database_url_key_still_loads_with_warning(write_config, caplog):
    # #796: clauster is SQLite-only now — the `database_url` field was removed from
    # ClausterConfig. Schema is additive-only (old configs must always validate against
    # newer versions), so a leftover `database_url` key from a pre-#796 config must LOAD,
    # not be rejected. But it must NOT be dropped silently ("fail closed, never silently"):
    # an operator who set a Postgres DSN would otherwise think their data lives there while
    # writes go to local SQLite — so load emits a WARNING naming the real data location.
    cfg_path = write_config("database_url: postgresql+psycopg://x/y\n")
    with caplog.at_level("WARNING", logger="clauster.config"):
        config = load_config(cfg_path)
    assert not hasattr(config, "database_url")
    msgs = [r.message for r in caplog.records]
    assert any("database_url" in m and "no longer supported" in m for m in msgs)
    assert any("clauster.db" in m for m in msgs)  # points at the real SQLite location


def test_legacy_database_url_env_var_warns(write_config, caplog, monkeypatch):
    # The removed field also drops CLAUSTER_DATABASE_URL from _env_leaf_map, so a DSN set
    # ONLY via the env override never reaches `raw` — the YAML-key check alone would miss it
    # and the setting would be silently ignored (Greptile #801 R2). The warning must fire for
    # the env path too, not just the YAML key.
    monkeypatch.setenv("CLAUSTER_DATABASE_URL", "postgresql+psycopg://x/y")
    cfg_path = write_config("")  # no database_url key in the file — env-only
    with caplog.at_level("WARNING", logger="clauster.config"):
        config = load_config(cfg_path)
    assert not hasattr(config, "database_url")
    msgs = [r.message for r in caplog.records]
    assert any("CLAUSTER_DATABASE_URL" in m and "no longer supported" in m for m in msgs)


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


def test_malformed_trusted_ip_rejected(write_config):
    # #549: a malformed reverse-proxy trusted-IP entry fails fast at load rather than being
    # silently skipped by auth.peer_trusted at runtime (a quiet no-op in an auth allowlist).
    with pytest.raises(ValueError):
        load_config(write_config('auth:\n  reverse_proxy:\n    trusted_ips: ["not-an-ip"]\n'))


def test_valid_trusted_ips_accepted(write_config):
    # Both a bare IP and a CIDR are accepted (auth.peer_trusted parses each via ip_network).
    config = load_config(
        write_config('auth:\n  reverse_proxy:\n    trusted_ips: ["10.0.0.5", "192.168.0.0/24"]\n')
    )
    assert config.auth.reverse_proxy.trusted_ips == ["10.0.0.5", "192.168.0.0/24"]


def test_require_hmac_defaults_true(write_config):
    # #367: the forward-auth toggle defaults to the higher-assurance HMAC-required mode,
    # so existing configs that never mention require_hmac keep today's behaviour.
    config = load_config(
        write_config(
            "auth:\n  enabled: true\n  reverse_proxy:\n    enabled: true\n"
            '    shared_secret: "k"\n    trusted_ips: ["10.0.0.1"]\n'
        )
    )
    assert config.auth.reverse_proxy.require_hmac is True


def test_hmac_mode_without_shared_secret_rejected(write_config):
    # #367: HMAC mode (default) is un-runnable without the key the proxy signs with —
    # fail closed at load rather than authenticate nobody.
    with pytest.raises(ValueError, match="shared_secret"):
        load_config(
            write_config(
                "auth:\n  enabled: true\n  reverse_proxy:\n    enabled: true\n"
                '    trusted_ips: ["10.0.0.1"]\n'
            )
        )


def test_hmac_mode_requires_trusted_ips(write_config):
    # #367: HMAC mode also gates on peer_trusted(peer_ip, trusted_ips) before checking the
    # signature, so an empty trusted_ips allowlist makes the proxy path admit no one even
    # with a valid shared_secret — fail closed at load rather than start silently inoperable.
    with pytest.raises(ValueError, match="trusted_ips"):
        load_config(
            write_config(
                "auth:\n  enabled: true\n  reverse_proxy:\n    enabled: true\n"
                '    shared_secret: "k"\n'
            )
        )


def test_header_only_mode_requires_trusted_ips(write_config):
    # #367: header-only forward-auth drops the HMAC requirement but the bare user_header is
    # forgeable — an empty trusted_ips allowlist in this mode is a footgun, so reject it.
    with pytest.raises(ValueError, match="trusted_ips"):
        load_config(
            write_config(
                "auth:\n  enabled: true\n  reverse_proxy:\n"
                "    enabled: true\n    require_hmac: false\n"
            )
        )


def test_header_only_mode_no_shared_secret_needed(write_config):
    # #367: with require_hmac false + a trusted_ips allowlist, no shared_secret is required.
    config = load_config(
        write_config(
            "auth:\n  enabled: true\n  reverse_proxy:\n"
            "    enabled: true\n    require_hmac: false\n"
            '    trusted_ips: ["10.0.0.1"]\n'
        )
    )
    assert config.auth.reverse_proxy.require_hmac is False
    assert config.auth.reverse_proxy.shared_secret is None


def test_disabled_reverse_proxy_skips_validation(write_config):
    # The cross-field validator only fires when enabled — a disabled block with neither
    # secret nor trusted_ips loads fine (the fields are inert).
    config = load_config(write_config("auth:\n  reverse_proxy:\n    require_hmac: false\n"))
    assert config.auth.reverse_proxy.enabled is False


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


def test_yaml_constructor_escapes_become_yaml_errors_without_the_scalar(write_config, tmp_path):
    # #1395: `yaml.safe_load` raises OUTSIDE `yaml.YAMLError` for a tag whose value does not
    # fit, and the payload IS the offending scalar. Every caller's YAML arm has to see these,
    # and none of them may see the value. The CLI arms print `str(exc)`, so the message must
    # also be non-empty — an empty one renders `clauster: config error:` with no diagnosis.
    canary = "FAKEFAKEFAKEFAKEfake42"
    for body in (
        f'auth:\n  token: !!int "{canary}"\n',  # ValueError
        f'auth:\n  token: !!float "{canary}"\n',  # ValueError, lowercased
        f'auth:\n  token: !!bool "{canary}"\n',  # KeyError
        f'a: !!timestamp "{canary}"\n',  # AttributeError
    ):
        cfg = tmp_path / "bad.yml"
        cfg.write_text(body, encoding="utf-8", newline="")
        with pytest.raises(UnfittingYamlTagError) as exc:
            load_config(cfg)
        assert canary not in str(exc.value) and canary.lower() not in str(exc.value)
        assert str(exc.value)


def test_deeply_nested_yaml_becomes_a_yaml_error(write_config, tmp_path):
    # RecursionError is not a `yaml.YAMLError` either, so it escaped every caller's arm.
    cfg = tmp_path / "bad.yml"
    cfg.write_text("[" * 5000, encoding="utf-8", newline="")
    with pytest.raises(TooDeeplyNestedYamlError, match="nested too deeply"):
        load_config(cfg)


def test_non_utf8_config_file_is_not_reclassified_as_a_yaml_error(tmp_path):
    # `read_text` sits OUTSIDE the reclassifying try on purpose: a non-UTF-8 file is an
    # encoding fault, and UnicodeDecodeError is a ValueError that would otherwise be
    # relabelled as an unfitting YAML tag.
    cfg = tmp_path / "bad.yml"
    cfg.write_bytes(b"projects_root: \xff\xfe\x80\n")
    with pytest.raises(UnicodeDecodeError):
        load_config(cfg)


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
        "host: 0.0.0.0\nauth:\n  enabled: true\n  reverse_proxy:\n    enabled: true\n"
        '    shared_secret: "k"\n    trusted_ips: ["10.0.0.1"]\n',
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
        "host: 0.0.0.0\nauth:\n  reverse_proxy:\n    enabled: true\n"
        '    shared_secret: "k"\n    trusted_ips: ["10.0.0.1"]\n',
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


@pytest.mark.parametrize("blank", ["''", "'   '", '"\t"'])
def test_blank_password_hash_normalizes_to_none(write_config, blank):
    # F3 regression (CWE-798): a blank / whitespace-only password_hash must normalize to
    # None, not survive as an empty string. An empty hash is falsy-yet-not-None and,
    # paired with auth.verify_password's dummy-hash timing guard, would let the
    # source-visible dummy password authenticate. Mirrors the api_token_hash case.
    config = load_config(write_config(f"auth:\n  enabled: true\n  password_hash: {blank}\n"))
    assert config.auth.password_hash is None


def test_blank_password_hash_env_var_normalizes_to_none(write_config, monkeypatch):
    # The env-var path assigns os.environ verbatim, so a present-but-empty
    # CLAUSTER_AUTH_PASSWORD_HASH must be normalized to None too (not applied as "").
    monkeypatch.setenv("CLAUSTER_AUTH_PASSWORD_HASH", "   ")
    config = load_config(write_config("auth:\n  enabled: true\n"))
    assert config.auth.password_hash is None


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


def test_log_level_defaults_to_info(write_config):
    # #993: absent from the file, the server keeps its historical INFO verbosity.
    assert load_config(write_config()).log_level == "info"


def test_log_level_from_file_and_env_override(write_config, monkeypatch):
    # #993 advertises `CLAUSTER_LOG_LEVEL` — a top-level `log_level` derives exactly that
    # name (`logs.level` would have derived CLAUSTER_LOGS_LEVEL instead).
    cfg_path = write_config("log_level: warning\n")
    assert load_config(cfg_path).log_level == "warning"
    monkeypatch.setenv("CLAUSTER_LOG_LEVEL", "debug")
    assert load_config(cfg_path).log_level == "debug"


def test_log_level_accepts_uppercase_names(write_config, monkeypatch):
    # `DEBUG` is what a Python-logging reader writes in a unit file; case-fold it rather
    # than fail startup on a spelling.
    monkeypatch.setenv("CLAUSTER_LOG_LEVEL", " DEBUG ")
    assert load_config(write_config()).log_level == "debug"


def test_log_level_rejects_unknown_name(write_config):
    # Fail closed and loudly: an unusable level must not silently fall back to info.
    with pytest.raises(ValueError, match="log_level"):
        load_config(write_config("log_level: trace\n"))


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


def test_env_override_list_field_splits_on_commas(write_config, monkeypatch):
    # #1072: a list[str] leaf used to take the raw string straight into the field, so
    # setting the advertised var crashed config load with a pydantic list_type error.
    cfg_path = write_config()
    monkeypatch.setenv(
        "CLAUSTER_AUTH_ALLOWED_ORIGINS", "http://localhost:7621, https://box.example.com"
    )
    config = load_config(cfg_path)
    assert config.auth.allowed_origins == ["http://localhost:7621", "https://box.example.com"]


def test_env_override_list_field_single_value_and_blanks(write_config, monkeypatch):
    # A single value is a one-element list, and a trailing comma must not produce an
    # empty-string origin (which would validate but never match a real Origin header).
    cfg_path = write_config()
    monkeypatch.setenv("CLAUSTER_CLONE_ALLOWED_SCHEMES", "https,")
    config = load_config(cfg_path)
    assert config.clone.allowed_schemes == ["https"]


def test_env_override_list_field_via_secret_file(write_config, monkeypatch, tmp_path):
    # The _FILE indirection has to split too — it shares the assignment path, so a
    # list leaf read from a file would otherwise land as a raw string and crash.
    secret_file = tmp_path / "origins"
    secret_file.write_text("http://a.example,http://b.example\n", encoding="utf-8")
    cfg_path = write_config()
    monkeypatch.setenv("CLAUSTER_AUTH_ALLOWED_ORIGINS_FILE", str(secret_file))
    config = load_config(cfg_path)
    assert config.auth.allowed_origins == ["http://a.example", "http://b.example"]


def test_env_override_list_field_secret_file_one_per_line(write_config, monkeypatch, tmp_path):
    # One-entry-per-line is the natural way to write a secret file, and splitting on commas
    # alone collapsed it into a single entry holding a raw newline. That entry matches no
    # real Origin, so the operator got "origin check failed" with nothing pointing at the
    # cause — a silent misconfiguration in exactly the Docker/secrets shape this serves.
    secret_file = tmp_path / "origins"
    secret_file.write_text("http://a.example\nhttp://b.example\n", encoding="utf-8")
    cfg_path = write_config()
    monkeypatch.setenv("CLAUSTER_AUTH_ALLOWED_ORIGINS_FILE", str(secret_file))
    config = load_config(cfg_path)
    assert config.auth.allowed_origins == ["http://a.example", "http://b.example"]


def test_env_override_empty_list_value_clears_rather_than_falling_through(
    write_config, monkeypatch
):
    # An empty value is an empty list, NOT "unset" — it does not fall back to the YAML
    # value. Pinned because a Compose file with `CLAUSTER_AUTH_ALLOWED_ORIGINS: ""` would
    # otherwise silently wipe a configured allowlist, and the origin gate now runs even
    # with auth off (#1070). Clearing fails closed (nothing is allowed), which is the safe
    # direction, but it must be deliberate and documented rather than accidental.
    cfg_path = write_config("auth:\n  allowed_origins:\n    - https://from-yaml.example\n")
    monkeypatch.setenv("CLAUSTER_AUTH_ALLOWED_ORIGINS", "")
    assert load_config(cfg_path).auth.allowed_origins == []


def test_dict_leaves_are_not_env_addressable(write_config, monkeypatch):
    # #1072: dict leaves can't be expressed by one env var, so they must not be mapped at
    # all. Before the fix they WERE mapped and assigned a raw string, turning the obvious
    # env var into a startup crash loop. Setting them now is inert, not fatal.
    from clauster.config import ClausterConfig, _env_leaf_map

    env_map = _env_leaf_map(ClausterConfig)
    for unmapped in ("CLAUSTER_PROJECTS", "CLAUSTER_CLAUDE_ENV", "CLAUSTER_WEBHOOKS_EVENTS"):
        assert unmapped not in env_map
    cfg_path = write_config()
    monkeypatch.setenv("CLAUSTER_PROJECTS", "anything")
    monkeypatch.setenv("CLAUSTER_CLAUDE_ENV", "FOO=bar")
    config = load_config(cfg_path)  # must not raise
    assert config.projects == {}
    assert config.claude.env == {}


def test_list_of_non_scalars_is_not_env_addressable():
    # A list of MODELS or nested containers has no delimited-string spelling. Mapping one
    # would advertise a var that assigns list[str] into it and crash load — the #1072 class
    # this map exists to remove. No such field exists today; this stops one being added
    # silently. Asserted on the classifier because the schema has no such leaf to drive it.
    from typing import Literal

    from clauster.config import ProjectConfig, _env_leaf_kind

    assert _env_leaf_kind(list[str]) == "list"
    # A list of Literals is still scalar-item — an enum-ish list stays env-settable.
    assert _env_leaf_kind(list[Literal["a", "b"]]) == "list"
    assert _env_leaf_kind(list[ProjectConfig]) is None
    assert _env_leaf_kind(list[dict[str, str]]) is None
    assert _env_leaf_kind(list[list[str]]) is None


def test_legacy_env_aliases_all_target_addressable_leaves():
    # A legacy alias pointing at a dropped dict leaf would assign a raw string and crash
    # config load. _apply_env_overrides skips such an alias, but an alias that SHOULD work
    # silently doing nothing is its own bug — so pin that every declared alias resolves.
    from clauster.config import _LEGACY_ENV_ALIASES, ClausterConfig, _env_leaf_map

    leaves = {path for path, _kind in _env_leaf_map(ClausterConfig).values()}
    for env_name, path in _LEGACY_ENV_ALIASES.items():
        assert path in leaves, f"{env_name} aliases {'.'.join(path)}, which is not env-addressable"


def test_legacy_alias_onto_unaddressable_leaf_is_skipped(write_config, monkeypatch):
    # The guard branch itself: an alias whose target is NOT env-addressable must be skipped,
    # not honored — honoring it would assign a raw string into a dict leaf and crash config
    # load, the same #1072 class. Driven with an injected alias because every real alias
    # targets a scalar, so the branch is otherwise unreachable.
    from clauster import config as config_mod

    monkeypatch.setitem(
        config_mod._LEGACY_ENV_ALIASES, "CLAUSTER_OLD_CLAUDE_ENV", ("claude", "env")
    )
    monkeypatch.setenv("CLAUSTER_OLD_CLAUDE_ENV", "FOO=bar")
    config = load_config(write_config())  # must not raise
    assert config.claude.env == {}  # skipped entirely, not partially applied


def test_every_list_env_var_validates_through_the_model(write_config, monkeypatch):
    # Companion to the coercion test below: that one bypasses model validation on purpose,
    # so it would pass even for a var that is fatal at load. This pushes each list var all
    # the way through load_config with a value valid for its field.
    samples = {
        "CLAUSTER_AUTH_ALLOWED_ORIGINS": "https://a.example,https://b.example",
        "CLAUSTER_AUTH_REVERSE_PROXY_TRUSTED_IPS": "10.0.0.0/8,192.168.1.1",
        "CLAUSTER_CLAUDE_PATH_APPEND": "/opt/bin,/usr/local/bin",
        "CLAUSTER_CLONE_ALLOWED_PRIVATE_CIDRS": "10.0.0.0/8",
        "CLAUSTER_CLONE_ALLOWED_SCHEMES": "https,ssh",
        "CLAUSTER_NOTIFICATIONS_URLS": "json://example.com",
        "CLAUSTER_TLS_HOSTNAMES": "clauster.example.com",
        "CLAUSTER_WEBHOOKS_URLS": "https://hook.example.com",
    }
    from clauster.config import ClausterConfig, _env_leaf_map

    list_vars = {e for e, (_p, k) in _env_leaf_map(ClausterConfig).items() if k == "list"}
    assert list_vars == set(samples), "a list env var gained/lost — add it to the samples"

    # A few leaves carry cross-field model rules that a lone value can't satisfy; that is
    # the model working, not a coercion failure, so the companion key goes in with it.
    companions = {
        # A tls block holding only hostnames trips "cert_file is required when
        # provision = off" before the list is ever inspected.
        "CLAUSTER_TLS_HOSTNAMES": {"CLAUSTER_TLS_PROVISION": "self-signed"},
    }

    for env_name, value in samples.items():
        monkeypatch.setenv(env_name, value)
        for extra_name, extra_value in companions.get(env_name, {}).items():
            monkeypatch.setenv(extra_name, extra_value)
        try:
            load_config(write_config())  # must not raise
        finally:
            monkeypatch.delenv(env_name, raising=False)
            for extra_name in companions.get(env_name, {}):
                monkeypatch.delenv(extra_name, raising=False)


def test_every_list_env_var_coerces_to_a_list(monkeypatch):
    # Guards the whole class rather than the one field #1072 was filed for: EVERY list-kind
    # var must reach the model as a list, so a future list field added to the schema fails
    # here instead of shipping as an advertised-but-crashing var.
    #
    # Asserted against _apply_env_overrides, not load_config, on purpose: the bug was the
    # shape handed to pydantic (str where list[str] was required). Going through load_config
    # would also drag in each field's CONTENT validation — trusted_ips wants IPs/CIDRs, not
    # URLs — so one sample value can't satisfy every field and a content rejection would
    # masquerade as the coercion regression this test exists to catch.
    from clauster.config import ClausterConfig, _apply_env_overrides, _env_leaf_map

    for env_name, (path, kind) in sorted(_env_leaf_map(ClausterConfig).items()):
        if kind != "list":
            continue
        monkeypatch.setenv(env_name, "one,two")
        try:
            data = _apply_env_overrides({})
        finally:
            monkeypatch.delenv(env_name, raising=False)
        value = data
        for key in path:
            value = value[key]
        assert value == ["one", "two"], f"{env_name} ({'.'.join(path)}) did not coerce to a list"


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


def test_usage_show_cost_false_resolves_to_mode_off(write_config, caplog):
    # Back-compat: show_cost=false with no explicit mode maps to "off" (deprecated path).
    with caplog.at_level(logging.WARNING, logger="clauster.config"):
        config = load_config(write_config("usage:\n  show_cost: false\n"))
    assert config.usage.show_cost is False
    assert config.usage.mode == "off"
    assert any("show_cost=false is deprecated" in r.message for r in caplog.records)


def test_usage_explicit_mode_wins_over_deprecated_show_cost(write_config, caplog):
    # usage.mode is authoritative: an explicit mode beats the deprecated show_cost=false alias.
    with caplog.at_level(logging.WARNING, logger="clauster.config"):
        config = load_config(write_config("usage:\n  mode: tokens\n  show_cost: false\n"))
    assert config.usage.mode == "tokens"  # mode wins
    assert any("show_cost=false is ignored" in r.message for r in caplog.records)


def test_usage_show_cost_false_and_explicit_mode_off_agree_silently(write_config, caplog):
    # show_cost=false + an explicit mode=off agree, so no deprecation warning fires (covers the
    # "both set, mode already off" branch — there is nothing to warn about).
    with caplog.at_level(logging.WARNING, logger="clauster.config"):
        config = load_config(write_config("usage:\n  mode: off\n  show_cost: false\n"))
    assert config.usage.mode == "off"
    assert not any("show_cost" in r.message for r in caplog.records)


def test_usage_currency_default_usd(write_config):
    assert load_config(write_config()).usage.currency == "USD"


def test_usage_currency_override_via_config(write_config):
    assert load_config(write_config("usage:\n  currency: EUR\n")).usage.currency == "EUR"


def test_notifications_defaults(write_config):
    n = load_config(write_config()).notifications
    assert n.enabled is False
    assert n.browser_enabled is False
    assert n.urls == []
    # crash is the historical default-ON event; every #541 event defaults OFF.
    assert n.notify_on_crash is True
    assert n.notify_on_ready is False
    assert n.notify_on_stop is False
    assert n.notify_on_permission is False
    assert n.notify_on_session_end is False
    assert n.notify_on_reconnect_failed is False


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


def test_notifications_new_event_toggles_and_browser_channel(write_config):
    extra = (
        "notifications:\n"
        "  browser_enabled: true\n"
        "  notify_on_ready: true\n"
        "  notify_on_stop: true\n"
        "  notify_on_permission: true\n"
        "  notify_on_session_end: true\n"
        "  notify_on_reconnect_failed: true\n"
    )
    n = load_config(write_config(extra)).notifications
    assert n.browser_enabled is True
    assert n.notify_on_ready is True
    assert n.notify_on_stop is True
    assert n.notify_on_permission is True
    assert n.notify_on_session_end is True
    assert n.notify_on_reconnect_failed is True


def test_notifications_event_enabled_maps_events_to_toggles(write_config):
    n = load_config(write_config("notifications:\n  notify_on_ready: true\n")).notifications
    assert n.event_enabled("crash") is True  # default-ON
    assert n.event_enabled("ready") is True  # explicitly enabled
    assert n.event_enabled("stop") is False  # default-OFF
    assert n.event_enabled("permission-needed") is False
    assert n.event_enabled("session-ended") is False
    assert n.event_enabled("reconnect-failed") is False
    assert n.event_enabled("totally-unknown") is False  # unknown -> False, never raises


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


def test_usage_currency_symbol_blank_normalized_to_default(write_config):
    # An empty / whitespace-only symbol must fall back to the default, not render a blank badge.
    empty = load_config(write_config("usage:\n  currency_symbol: ''\n")).usage
    assert empty.currency_symbol is None
    assert empty.effective_symbol == "$"
    ws = load_config(write_config("usage:\n  currency_symbol: '   '\n")).usage
    assert ws.currency_symbol is None
    assert ws.effective_symbol == "$"


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


def test_pty_screen_enabled_defaults_off_and_parses(write_config):
    # #534: the live read-only pty-screen tap is off by default and opt-in via config.
    assert (
        load_config(write_config("claude:\n  binary: claude\n")).claude.pty_screen_enabled is False
    )
    on = load_config(write_config("claude:\n  pty_screen_enabled: true\n"))
    assert on.claude.pty_screen_enabled is True


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


def test_new_launch_mode_env_var_applies_without_deprecation_warning(
    write_config, monkeypatch, caplog
):
    # The new env var alone applies cleanly: the legacy-alias loop sees the new var set and
    # the old one unset, so it skips silently (no deprecation warning for a non-legacy user).
    import logging

    monkeypatch.setenv("CLAUSTER_CLAUDE_LAUNCH_MODE", "pty")
    with caplog.at_level(logging.WARNING, logger="clauster.config"):
        config = load_config(write_config())
    assert config.claude.launch_mode == "pty"
    assert not any(
        "deprecated" in r.message or "resume_mode" in r.message.lower() for r in caplog.records
    )


def test_legacy_resume_mode_file_env_alias_maps_to_launch_mode(
    write_config, monkeypatch, tmp_path
):
    # The legacy alias also honors the CLAUSTER_<X>_FILE secret-indirection form.
    secret = tmp_path / "mode"
    secret.write_text("pty\n", encoding="utf-8")  # trailing newline is stripped by the reader
    monkeypatch.setenv("CLAUSTER_CLAUDE_RESUME_MODE_FILE", str(secret))
    assert load_config(write_config()).claude.launch_mode == "pty"


def test_state_dir_validator_passes_through_non_path_types(projects_root):
    # config.py 1262->1264: the expanduser pre-validator passes a non-str/Path value
    # through untouched so pydantic's own type error surfaces (a masked crash inside
    # the validator would hide WHICH field was wrong).
    import pytest
    from pydantic import ValidationError

    from clauster.config import ClausterConfig

    with pytest.raises(ValidationError, match="state_dir"):
        ClausterConfig(projects_root=projects_root, state_dir=12345)


# ----- ui — web-dashboard kill switch (#806) --------------------------------


def test_ui_enabled_default_true(write_config):
    # Default true = zero behavior change unless an operator opts out.
    assert load_config(write_config()).ui.enabled is True


def test_ui_enabled_false_via_config(write_config):
    config = load_config(write_config("ui:\n  enabled: false\n"))
    assert config.ui.enabled is False


def test_ui_enabled_env_override(write_config, monkeypatch):
    cfg_path = write_config()
    monkeypatch.setenv("CLAUSTER_UI_ENABLED", "false")
    assert load_config(cfg_path).ui.enabled is False
