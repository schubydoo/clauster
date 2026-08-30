# Fuzzing (ClusterFuzzLite + Atheris)

Coverage-guided fuzz harnesses for clauster's untrusted-input parsers. They run
in CI via [ClusterFuzzLite](https://google.github.io/clusterfuzzlite/) two ways:
per-PR over the changed code (`.github/workflows/cflite_pr.yml`, `address`
sanitizer, ~180s) and an every-other-day scheduled batch over **every** harness
(`.github/workflows/cflite_batch.yml`, 375s) whose corpus **persists across
runs** in a private storage repo, so coverage compounds over time. Note
`fuzz-seconds` is a **total** budget CFLite splits across the harnesses, not a
per-target one — adding a harness thins every harness's slice rather than
lengthening the run, so raise it in step with the harness count. A weekly cron
(`.github/workflows/cflite_cron.yml`) prunes that corpus and replays it to publish
per-harness edge counts (see [below](#reading-the-weekly-coverage-signal)). All are
informational — never block a merge; a reproducible crash surfaces as SARIF in the
Security tab. Build config lives in [`.clusterfuzzlite/`](../.clusterfuzzlite/).

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
| `auth_headers_fuzzer.py` | `auth.verify_proxy_hmac` + `auth.parse_bearer` | the two **pre-authentication** header parsers on the same boundary as `normalize_origin`. `verify_proxy_hmac` must return `False` on any malformation, never raise (it already 500'd once on a non-ASCII signature via `compare_digest`'s `TypeError`). Also builds a correctly signed header per input so the accept path is exercised, not just the reject path |
| `pty_login_scan_fuzzer.py` | `pty_screen.extract_authorize_url` + `extract_osc8_hyperlinks` + `extract_oauth_token` | the scanners `login_shepherd` runs over `claude auth login` / `setup-token` terminal output to find the authorize URL an operator is told to click. Beyond crashes it asserts two selection properties — anti-decoy (when any candidate's path is a real authorize endpoint, the winner's must be too) and the stricter bar a *hidden* OSC 8 target must clear (known auth host **and** authorize path). Both are judged by predicates the harness restates itself, so a misclassification inside `pty_screen` can't shift both sides of the comparison together |

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

## Reading the weekly coverage signal

⚠️ **No HTML line-coverage report has landed yet.** The weekly cron's `coverage` job
has in practice been a *corpus replay*: it rebuilds the harnesses, runs the whole
persisted corpus through each one, and pushes the raw libFuzzer output to the corpus
repo's `gh-pages` branch. Verified directly against that branch through 2026-08-30 —
no `coverage/latest/report/` directory, and a weekly upload commit touching only the
per-harness logs.

⚠️ **This was never "Python can't do coverage reports."** oss-fuzz's base-runner
`coverage` script has a coverage.py branch that builds an `htmlcov` report and moves it
to `$COVERAGE_OUTPUT_DIR/report/$PLATFORM` — exactly the `report/linux/` path the
workflow comment used to promise. We never reached it: the cron's `run_fuzzers` step
passed no `language` (only the *build* step did) and that input defaults to `c++`, so
the script took its LLVM branch. The uploaded logs proved it — they carried
`-merge=1` `MERGE-INNER`/`MERGE-OUTER` lines, which only the C/C++ path emits. Our
Atheris wrappers produce no `.profraw`, so that path yielded nothing, and the job
stays green either way because ClusterFuzzLite discards the coverage script's output.

`language: python` is now set on every `run_fuzzers` step
([issue 1327](https://github.com/schubydoo/clauster/issues/1327)), but it is
*necessary*, not proven *sufficient* — the Python report stage also wants a
`${fuzzer}.pkg.deps.zip` out of the coverage *build*, which has never been observed
here. (The other input it needs, `.coverage_$target`, the run step writes itself.)

**On the next scheduled cron, check `gh-pages` for two things:** whether
`coverage/latest/report/` appeared, and whether the per-harness logs below survived.
Those logs are the only fuzz-coverage signal we have; if the Python path stops
producing them without producing a report, revert `language` on the *coverage job
only* — the PR, batch and prune jobs need it for the crash-reproduce timeout
regardless. Wait for the cron rather than dispatching it by hand: a dispatch runs
unmerged workflow code with the write PAT and pushes to the real `gh-pages`.

Expect the artefact *shape* to change on the Python path even when it works:
`<harness>.log` is still written, but `_error.log` and the `MERGE-` lines are
C/C++-only and should disappear, and `fuzzer_stats/<harness>.json` becomes a dummy
`{}`. Update the table below once that is observed.

What has landed to date (under the `c++` default):

| Path (on `gh-pages`) | What it is |
| --- | --- |
| `coverage/latest/logs/<harness>.log` | that harness's libFuzzer replay log (stdout and stderr together); its last `DONE cov: E ft: F` line is the edge (`E`) and feature (`F`) count reached over the whole corpus. Not the last line of the file — `MERGE-OUTER:` lines follow it, so grep for `DONE` rather than reaching for `tail -1` |
| `coverage/latest/logs/<harness>_error.log` | always empty, and empty is the good case — the C/C++ path creates it by redirecting a `grep` for libFuzzer `ERROR:` lines, so an empty file means no crash line matched |
| `coverage/latest/fuzzer_stats/coverage_targets.txt` | the harnesses the replay covered |

The corpus repo is private, so read them from a clone rather than a Pages URL:

```sh
git clone --depth 1 -b gh-pages git@github.com:schubydoo/clauster-fuzz-corpus.git
cd clauster-fuzz-corpus
for f in coverage/latest/logs/*_fuzzer.log; do
  printf '%-34s %s\n' "$(basename "$f" .log)" "$(grep -o 'DONE .*' "$f" | tail -1)"
done | sort
```

**What the numbers do and don't tell you.** `cov:` counts *instrumented edges the
corpus reaches* — not lines, not a percentage — and is only meaningful against the
same harness's own previous runs; a harness over a short validator legitimately sits
an order of magnitude below one over an NDJSON demuxer. Two things are worth acting
on:

- **A harness whose `cov:` stops growing** while its target keeps changing — its
  corpus is likely stuck behind a guard clause. Give it a seed corpus or a
  dictionary (below).
- **A module that appears nowhere in `coverage_targets.txt`** — nothing fuzzes it at
  all. That absence, rather than a 0% cell in a report, is the signal that a new
  harness is owed.

## Adding a harness

Drop a `*_fuzzer.py` file here with a `TestOneInput(data: bytes)` entry point (see
the existing ones). `build.sh` discovers `fuzz/*_fuzzer.py` automatically — no
workflow change needed. Good targets: pure functions that accept untrusted/
structured input and are expected never to crash.

Two traps worth knowing before you measure anything:

- ⚠️ **Put every import the harness needs inside the `atheris.instrument_imports()`
  block** — including stdlib ones like `urlsplit`, `hmac` and `hashlib`. An import at
  module scope loads the module *before* Atheris can instrument it, and the target's
  own later import then reuses the uninstrumented copy. Measured: hoisting
  `from urllib.parse import urlsplit` out of `pty_login_scan_fuzzer` dropped it from
  ~210 edges to 52 — `urlsplit` *is* the parser behind the helpers that harness drives,
  so the loss is most of its signal — and hoisting `hmac`/`hashlib` out of
  `auth_headers_fuzzer` dropped it from 32 to 22, there just losing the stdlib digest
  code (`clauster.auth` is traced either way, being imported in-block in both layouts).
  Both numbers are from a direct `python fuzz/…` run; under
  ClusterFuzzLite the harness is PyInstaller-frozen, so if that bootstrap has already
  imported the module the in-block import degrades to a lookup and instrumentation is
  lost again. Atheris prints `INFO: Instrumenting <module>` at startup — check for it
  in the run log rather than assuming.
- ⚠️ **Give libFuzzer a scratch corpus directory, not `fuzz/seeds/<name>/`.** It writes
  every retained input into the *first* corpus dir on the command line, so running
  `python fuzz/x_fuzzer.py fuzz/seeds/x_fuzzer` buries the curated seeds under
  thousands of generated files. Use `python fuzz/x_fuzzer.py /tmp/corp fuzz/seeds/x_fuzzer`.

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

`redact_fuzzer`, `parse_markers_fuzzer` and `pty_login_scan_fuzzer` ship both (their
regexes need structured tokens the random fuzzer never synthesises);
`auth_headers_fuzzer` ships a dictionary only. The rest grow a corpus fine from
structural input and need neither.

The effect is not marginal. `pty_login_scan_fuzzer` sits at **11 edges** on random bytes
— it never synthesises `https://`, so it fuzzes only the no-match path — and passes
**200** within the first million iterations once its dictionary and twelve seeds are in
play. Measure before deciding a harness "needs neither": run it locally for a few
hundred thousand iterations and read the `cov:` figure.

If a harness asserts a *property* rather than only "does not crash", prove the
assertion can fail before trusting it. Break the implementation on purpose (monkeypatch
the function under test to return something wrong) and check the harness raises — an
oracle that has never fired is indistinguishable from no oracle at all.
