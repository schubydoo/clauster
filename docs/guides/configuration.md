# Configuring Clauster

All settings live in `clauster.yml`.
[`clauster.yml.example`](https://github.com/schubydoo/clauster/blob/main/clauster.yml.example)
is a lean starter (the common keys, with every other section shown commented at
its default). This page covers how the file is found and how to override it —
for what every key means, see the
[configuration reference](../reference/config.md), and for changing settings
from the browser, [the in-app config editor](config-editor.md).

## Loading & overrides

Clauster searches for a config in this order (first file that exists wins):

1. The path passed to `clauster run -c <path>` (explicit).
2. `$CLAUSTER_CONFIG`
3. `./clauster.yml`
4. `$CLAUSTER_HOME/clauster.yml`

If none is found, startup fails with a `FileNotFoundError` listing the paths it
searched.

**Environment overrides.** Any *scalar* key is overridable by an environment
variable named `CLAUSTER_<UPPER_SNAKE_PATH>` — the dotted path uppercased and
joined with underscores. For example:

- `auth.enabled` → `CLAUSTER_AUTH_ENABLED`
- `auth.password_hash` → `CLAUSTER_AUTH_PASSWORD_HASH`
- `claude.launch_mode` → `CLAUSTER_CLAUDE_LAUNCH_MODE` *(full dotted path — see note)*

!!! note "Env mapping is by leaf path"
    The mapping recurses nested models and uses the *full dotted path*, so
    `claude.launch_mode` maps to `CLAUSTER_CLAUDE_LAUNCH_MODE`. `dict`/`list`
    leaves (e.g. `projects`, `reverse_proxy.trusted_ips`,
    `clone.allowed_private_cidrs`) **cannot** be set via env — a single env var
    can't express them unambiguously; set those in the YAML file.

**Secret files (`*_FILE`).** Every `CLAUSTER_<X>` variable also accepts a
`CLAUSTER_<X>_FILE` form that reads the value from a file instead of the
environment — for secrets that Docker / Podman / Kubernetes / Vault render to
files under `/run/secrets` rather than env vars, keeping them out of the process
environment. The file's contents win over the plain variable, and trailing
whitespace (e.g. a trailing newline) is stripped. An unreadable `_FILE` path is a
fatal misconfiguration (it does not silently fall back). The session secret has
its own `CLAUSTER_SESSION_SECRET_FILE` (it is read outside the config schema):

- `auth.password_hash` → `CLAUSTER_AUTH_PASSWORD_HASH_FILE`
- `auth.api_token_hash` → `CLAUSTER_AUTH_API_TOKEN_HASH_FILE`
- `observability.metrics_token_hash` → `CLAUSTER_OBSERVABILITY_METRICS_TOKEN_HASH_FILE`
- session secret → `CLAUSTER_SESSION_SECRET_FILE`

```yaml
# docker-compose: render a secret to a file and point clauster at it
services:
  clauster:
    environment:
      CLAUSTER_AUTH_PASSWORD_HASH_FILE: /run/secrets/clauster_pw_hash
    secrets:
      - clauster_pw_hash

secrets:
  clauster_pw_hash:
    file: ./secrets/pw_hash.txt
```

**Schema is additive-only.** Old configs always validate against newer versions;
unknown per-project keys are ignored.

## Minimal example

```yaml
# loopback, no auth needed
projects_root: ~/code
```

## LAN example (password auth)

```yaml
projects_root: ~/code
host: 0.0.0.0
port: 7621
auth:
  enabled: true
  password_required: true
  password_hash: "$argon2id$v=19$..."   # from `clauster hash-password`
  cookie_secure: always                 # if no TLS-terminating proxy
```
