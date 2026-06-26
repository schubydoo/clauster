# Fuzzing (ClusterFuzzLite + Atheris)

Coverage-guided fuzz harnesses for clauster's untrusted-input parsers. They run
in CI via [ClusterFuzzLite](https://google.github.io/clusterfuzzlite/) two ways:
per-PR over the changed code (`.github/workflows/cflite_pr.yml`, `address`
sanitizer, ~180s) and an every-other-day scheduled batch over **every** harness
(`.github/workflows/cflite_batch.yml`, 300s each) whose corpus **persists across
runs** in a private storage repo, so coverage compounds over time. A weekly cron
(`.github/workflows/cflite_cron.yml`) prunes that corpus and publishes a coverage
report. All are informational — never block a merge; a reproducible crash surfaces
as SARIF in the Security tab. Build config lives in [`.clusterfuzzlite/`](../.clusterfuzzlite/).

## Harnesses

| File | Target | Why it's fuzzed |
| --- | --- | --- |
| `parse_markers_fuzzer.py` | `bridge_log.parse_bridge_markers` | regex parse of raw bridge debug output; must tolerate any input without raising (cf. the #122 `UnicodeDecodeError` class) |
| `redact_fuzzer.py` | `redact.sanitize_line` | ANSI-strip + ID/secret redaction over untrusted log lines; checks for crashes/ReDoS **and** asserts no bare `env_`/`session_`/`cse_` id leaks through |
| `validate_clone_url_fuzzer.py` | `provisioning.validate_clone_url` | the SSRF/URL guard; DNS is monkeypatched so fuzzing is deterministic + offline. Expected `ProvisionError` rejections are caught; any other exception is a bug |
| `claustrum_client_fuzzer.py` | `claustrum_client.ClaustrumClient._dispatch` + `ProcessStream.feed` | NDJSON demux + base64 line-reassembly of daemon stream frames (which relay attacker-influenceable agent stdout); must tolerate any bytes/shape without raising |
| `normalize_origin_fuzzer.py` | `auth.normalize_origin` | the CSRF/CORS origin gate parses the raw inbound `Origin` header; must reject a malformed origin (→ 403), never let it 500. Found + fixed a `urlsplit().port` `ValueError` (the #122 `.port` class) |
| `parse_agents_json_fuzzer.py` | `inspector.parse_agents_json` | parses external `claude agents --json` stdout (Anthropic-controlled, version-dependent); stays strict on malformed JSON (`JSONDecodeError` caught), any other escape is a bug |
| `supervisor_job_from_state_fuzzer.py` | `supervisor._job_from_state` | coerces the agent-view daemon's churning on-disk state into a `BackgroundJob`; total over any dict — must never raise |
| `load_trusted_paths_fuzzer.py` | `discovery._load_trusted_paths` | parses the user-editable `~/.claude.json`; must degrade any malformed file to an empty set. Found + fixed a non-dict-JSON `AttributeError` (the #122 class) |

## Running a harness locally

Atheris has no Windows wheels. On Linux, install it via the `fuzz` extra (it is
kept out of `dev` on purpose and marked linux-only so the default sync stays
cross-platform); on macOS, `pip install atheris` directly. Quick smoke run:

```sh
uv pip install '.[fuzz]'            # clauster + atheris (Linux)
python fuzz/redact_fuzzer.py -atheris_runs=100000     # finite run
# or let it run until a crash / Ctrl-C:
python fuzz/parse_markers_fuzzer.py
```

A crash writes a `crash-<sha1>` reproducer file; re-run the harness with that file
as an argument to reproduce: `python fuzz/redact_fuzzer.py crash-abc123`.

## Adding a harness

Drop a `*_fuzzer.py` file here with a `TestOneInput(data: bytes)` entry point (see
the existing ones). `build.sh` discovers `fuzz/*_fuzzer.py` automatically — no
workflow change needed. Good targets: pure functions that accept untrusted/
structured input and are expected never to crash.

## Seed corpora & dictionaries

A harness whose interesting branches are gated on **literal tokens** (the
redaction id/secret prefixes, the `[bridge:init]`-style markers) won't reach them
from Atheris' random `FuzzedDataProvider` bytes alone — it sits at a 0-element
corpus, fuzzing only the no-match path. Bootstrap it with either or both, picked
up automatically by `build.sh`:

- **Seed corpus** — representative inputs in `fuzz/seeds/<harness_name>/` (one file
  per input). `build.sh` zips them to `$OUT/<harness_name>_seed_corpus.zip`.
- **Dictionary** — literal tokens in `fuzz/dicts/<harness_name>.dict` (libFuzzer
  format: `"token"` per line, `\xNN` escapes). `build.sh` copies it to
  `$OUT/<harness_name>.dict`, which the fuzzer auto-loads as `-dict=`.

`redact_fuzzer` and `parse_markers_fuzzer` ship both (their regexes need structured
tokens the random fuzzer never synthesises); the others grow a corpus fine from
structural input and need neither.
