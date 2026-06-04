# Fuzzing (ClusterFuzzLite + Atheris)

Coverage-guided fuzz harnesses for clauster's untrusted-input parsers. They run
in CI via [ClusterFuzzLite](https://google.github.io/clusterfuzzlite/) on every
PR (`.github/workflows/cflite_pr.yml`, `address` sanitizer, ~180s, informational
— never blocks a merge). Build config lives in [`.clusterfuzzlite/`](../.clusterfuzzlite/).

## Harnesses

| File | Target | Why it's fuzzed |
| --- | --- | --- |
| `parse_markers_fuzzer.py` | `bridge_log.parse_bridge_markers` | regex parse of raw bridge debug output; must tolerate any input without raising (cf. the #122 `UnicodeDecodeError` class) |
| `redact_fuzzer.py` | `redact.sanitize_line` | ANSI-strip + ID/secret redaction over untrusted log lines; checks for crashes/ReDoS **and** asserts no bare `env_`/`session_`/`cse_` id leaks through |
| `validate_clone_url_fuzzer.py` | `provisioning.validate_clone_url` | the SSRF/URL guard; DNS is monkeypatched so fuzzing is deterministic + offline. Expected `ProvisionError` rejections are caught; any other exception is a bug |

## Running a harness locally

Atheris only builds on Linux/macOS. Quick smoke run:

```sh
pip install atheris
pip install .                       # install clauster into the env
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
