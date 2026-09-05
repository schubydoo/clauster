# Fuzzing (ClusterFuzzLite + Atheris)

Coverage-guided fuzz harnesses for clauster's untrusted-input parsers. They run
in CI via [ClusterFuzzLite](https://google.github.io/clusterfuzzlite/) two ways:
per-PR over the changed code (`.github/workflows/cflite_pr.yml`, `address`
sanitizer, ~180s) and an every-other-day scheduled batch over **every** harness
(`.github/workflows/cflite_batch.yml`, 666s) whose corpus **persists across
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
| `redact_fuzzer.py` | `redact.sanitize_line` | ANSI-strip + ID/secret redaction over untrusted log lines; checks for crashes/ReDoS **and** asserts no bare `env_`/`session_`/`cse_` id leaks through. Both leak properties are judged on the **rendered** output — escape sequences and invisible control characters contribute nothing to a `<pre>` — because asserting on the raw string is precisely what let issue 1370's control-char split hide. The second property is the one that can see an escape **weld** (issue 1379): a `\b`-anchored assertion structurally cannot, since the weld's whole effect is to delete that boundary, so it instead requires every identifier still readable in the output to be explained by an *unreachable* occurrence in the input (start neither a word boundary nor the offset of a removal). The render model and the identifier shape are restated in the harness, not imported, so a mistake inside `redact` cannot move both sides of the comparison. It found two real defects in the fix for 1379 that review had missed — a deleted **trailing** boundary, and a leaked identifier hidden inside a longer unreachable one by a non-overlapping scan |
| `validate_clone_url_fuzzer.py` | `provisioning.validate_clone_url` | the SSRF/URL guard; DNS is monkeypatched so fuzzing is deterministic + offline. Expected `ProvisionError` rejections are caught; any other exception is a bug |
| `claustrum_client_fuzzer.py` | `claustrum_client.ClaustrumClient._dispatch` + `ProcessStream.feed` | NDJSON demux + base64 line-reassembly of daemon stream frames (which relay attacker-influenceable agent stdout); must tolerate any bytes/shape without raising |
| `normalize_origin_fuzzer.py` | `auth.normalize_origin` | the CSRF/CORS origin gate parses the raw inbound `Origin` header; must reject a malformed origin (→ 403), never let it 500. Found + fixed a `urlsplit().port` `ValueError` (the #122 `.port` class) |
| `parse_agents_json_fuzzer.py` | `inspector.parse_agents_json` | parses external `claude agents --json` stdout (Anthropic-controlled, version-dependent); stays strict on malformed JSON (`JSONDecodeError` caught), any other escape is a bug |
| `supervisor_job_from_state_fuzzer.py` | `supervisor._job_from_state` | coerces the agent-view daemon's churning on-disk state into a `BackgroundJob`; total over any dict — must never raise |
| `load_trusted_paths_fuzzer.py` | `discovery._load_trusted_paths` | parses the user-editable `~/.claude.json`; must degrade any malformed file to an empty set. Found + fixed a non-dict-JSON `AttributeError` (the #122 class) |
| `auth_headers_fuzzer.py` | `auth.verify_proxy_hmac` + `auth.parse_bearer` | the two **pre-authentication** header parsers on the same boundary as `normalize_origin`. `verify_proxy_hmac` must return `False` on any malformation, never raise (it already 500'd once on a non-ASCII signature via `compare_digest`'s `TypeError`). Also builds a correctly signed header per input so the accept path is exercised, not just the reject path |
| `redact_secret_lines_fuzzer.py` | `config_write.redact_secret_lines` | **differential.** The line-oriented redaction every free-text read path runs over a file that arrived with a cloned repo. Its three scanners are hand-rolled *linear* rewrites of regexes that were quadratic (`py/polynomial-redos`), each documented as reproducing its regex exactly — a claim that, if wrong, under-masks and leaks while still returning a well-formed string a crash harness would accept. So the harness re-derives the answer from the **replaced regexes** and compares byte for byte, plus: no `${…}` from the input survives into the output, the line count and endings are preserved, and the differential holds again on the already-redacted output. Deliberately *not* asserted (each would report a documented design choice as a crash): idempotence, and the absence of a `scheme://user@` shape in the output — the URL replacement carries its own `@`, so it manufactures shapes out of already-masked material |
| `usage_line_to_turn_fuzzer.py` | `usage._line_to_turn` | one transcript JSONL line → the redacted turn the browser renders. Every byte originated in a model response or tool output. Must never raise on a malformed line; also asserts the turn's shape, that the documented skip rules decide `None`, and — like `redact_fuzzer` — that no bare `env_`/`session_`/`cse_` id survives into a rendered field |
| `load_settings_json_obj_fuzzer.py` | `config_write.load_settings_json_obj` | the read side of the **code-executing write tier**: the `.claude/settings.json` that arrives with a cloned repo, parsed before anything merges into it. Had no direct unit tests. Only `InvalidCandidateError` may escape; accept/reject and the parsed value are differentiated against plain `json.loads` |
| `parse_frontmatter_fuzzer.py` | `config_write_subagents.parse_frontmatter` + `config_write_skills.parse_frontmatter` | the two paired YAML frontmatter splitters on the write tier, in **one** harness so a drift shows up as a finding. Per parser: only `InvalidCandidateError` escapes, the header is a mapping, the body is a verbatim suffix of the input. Across the pair: when both accept, the header **and** the body must match — the tool-grant is the part with consequences, and both parsers now share one fence and one YAML load (`config_write.FRONTMATTER_RE` / `load_frontmatter_yaml`) so they cannot drift |
| `hosted_redact_obj_fuzzer.py` | `hosted._redact_obj` | invariant 4's enforcement point on every stream-json frame relayed to a browser. Frames are generated structurally, deliberately including chains past `_REDACT_MAX_DEPTH`, and checked for identifier leaks in string values, a genuinely capped output depth, and a preserved frame shape |
| `hosted_instance_from_record_fuzzer.py` | `hosted.HostedManager._instance_from_record` | rebuilds a hosted session from `hosted_state.json` on restart, where one bad field used to abort the reattach for every session. Oracle is the **round trip** its docstring promises ("inverse of `_record`"): a fixed point from the first pass on. Also pins `daemon_last_seq` to a non-negative non-bool int, and — since issue 1343 made the mapper total — that no `ValidationError` escapes it |
| `is_session_not_found_fuzzer.py` | `code_sessions._is_session_not_found` | a 404 body from `api.anthropic.com` — genuinely remote bytes, on a dated beta whose shape is expected to churn. Returning `True` clears a bridge pointer, so the harness asserts **fail-closed**: a `True` requires a `not_found_error`/`session` pair to actually exist somewhere in the parsed body |
| `pty_screen_feed_fuzzer.py` | `PtyScreen.feed` → `find_authorize_url` / `find_oauth_token` / `find_session_id` / `frame` | the **stateful** seam `pty_login_scan_fuzzer` leaves out (PR 1331 review): chunked feeds, OSC 8 carry across read boundaries, and the visible-to-hidden fallback, over both production configurations. Oracle is chunk-boundary invariance of the OSC 8 reassembly, plus frame geometry and the no-bare-identifier property on the **delivered** row — after `PtyScreen.frame`'s post-redaction width re-fit (#1359), not just `redact_screen_text`'s output. The former `_swallows_an_opener` OSC 8 exclusion (#1356) and the `len(redacted_row) <= cols` exemption are now **closed** by those two fixes and the oracle runs unconditionally; read the module docstring before widening any remaining exclusion |
| `pty_login_scan_fuzzer.py` | `pty_screen.extract_authorize_url` + `extract_osc8_hyperlinks` + `extract_oauth_token` | the scanners `login_shepherd` runs over `claude auth login` / `setup-token` terminal output to find the authorize URL an operator is told to click. Beyond crashes it asserts two selection properties — anti-decoy (when any candidate's path is a real authorize endpoint, the winner's must be too) and the stricter bar a *hidden* OSC 8 target must clear (known auth host **and** authorize path). Both are judged by predicates the harness restates itself, so a misclassification inside `pty_screen` can't shift both sides of the comparison together |

## Running a harness locally

Atheris ships in the `dev` extra on Linux, under a `sys_platform == 'linux'` marker (it
has no Windows wheels) that keeps `uv lock` and the Windows and macOS CI cells green. So
`uv sync --extra dev` installs it on Linux and the harness smoke test runs. The standalone
`fuzz` extra also carries it, for a fuzz-only install. On macOS, `pip install atheris`
directly. `pty_screen_feed_fuzzer` also
needs the optional `pty` extra (pyte) — without it `PtyScreen` raises
`PyteUnavailableError` on every input, so `.clusterfuzzlite/build.sh` installs
`clauster[pty]` too. Quick smoke run:

```sh
uv pip install '.[fuzz,pty]'        # clauster + atheris (Linux) + pyte
python fuzz/redact_fuzzer.py -atheris_runs=100000     # finite run
# or let it run until a crash / Ctrl-C:
python fuzz/parse_markers_fuzzer.py
```

A crash writes a `crash-<sha1>` reproducer file; re-run the harness with that file
as an argument to reproduce: `python fuzz/redact_fuzzer.py crash-abc123`.

## Reading the weekly coverage signal

The weekly cron `coverage` job rebuilds the harnesses with the coverage sanitizer,
replays the whole persisted corpus through each one, and pushes three kinds of
artifact to the corpus repo `gh-pages` branch under `coverage/latest/`.

The HTML line-coverage report is for people. It lives at
`coverage/latest/report/linux/index.html`, with `summary.json` and
`textcov_reports/all_cov.json` beside it. Read it as human coverage: the clauster
lines the corpus reaches. It lands only because `language: python` is set on the
coverage `run_fuzzers` step
([PR 1338](https://github.com/schubydoo/clauster/pull/1338), on top of
[issue 1327](https://github.com/schubydoo/clauster/issues/1327)). Before that the step
defaulted to `c++` and took the LLVM branch of the base-runner `coverage` script, which
the Atheris wrappers feed nothing, so no report appeared through 2026-08-30. The first
real report came from a 2026-09-04 dispatch from `main`, not a scheduled Sunday run.

The per-target `fuzzer_stats/<harness>.json` files are for cifuzz. The per-PR job runs
only the harnesses a change affects, and cifuzz reads these files to decide which. On
the Python path the base-runner writes each one as a dummy `{}`, so cifuzz saw no
coverage and kept every harness. The coverage job now rebuilds each file into real
per-target line coverage from the base-runner `coverage_d_<harness>` data (see
[issue 1503](https://github.com/schubydoo/clauster/issues/1503) and
`scripts/gen_fuzzer_stats.py`). A missing or empty file stays safe: cifuzz reads it as
"no coverage" and keeps the harness, so pruning never drops a harness it lacks data for.

The per-harness replay logs are the edge counts. Each `coverage/latest/logs/<harness>.log`
records that harness's libFuzzer replay. Its last `DONE cov: E ft: F` line is the edge
(`E`) and feature (`F`) count reached over the whole corpus. A harness pinned at a
handful of edges is one whose corpus never gets past its guard clause. A module with no
harness does not appear at all.

Operational notes:

- A green cron job does not prove the upload. ClusterFuzzLite discards the coverage
  script's output, so read `gh-pages` to confirm, not the job status.
- Do not dispatch the prune or coverage job from a feature branch. The write PAT is an
  environment secret in `fuzz-corpus` with a `main`-only deployment-branch policy that
  GitHub enforces server-side, so the job's `if: github.ref == 'refs/heads/main'` guard
  matches it. A dispatch from `main` runs and writes the real `gh-pages`.
- If the Python path stops producing the logs, revert `language` on the coverage job
  only. The PR and batch jobs need it for the crash-reproduce timeout. On prune it is
  inert, because cifuzz's prune path returns `testcase=None` and never reaches the
  reproduce step.

The artifacts on `gh-pages`, under `coverage/latest/`:

| Path | What it is |
| --- | --- |
| `report/linux/index.html` | the HTML line-coverage report, with `summary.json` and `textcov_reports/all_cov.json` beside it |
| `logs/<harness>.log` | that harness's libFuzzer replay log (stdout and stderr together). Its last `DONE cov: E ft: F` line is the edge (`E`) and feature (`F`) count over the whole corpus. `MERGE-OUTER:` lines follow it, so grep for `DONE` rather than `tail -1` |
| `fuzzer_stats/<harness>.json` | per-target line coverage in the llvm-cov shape cifuzz reads for code-change pruning, rebuilt from `coverage_d_<harness>` by `scripts/gen_fuzzer_stats.py` (the base-runner itself writes a dummy `{}` here) |
| `fuzzer_stats/coverage_targets.txt` | the harnesses the replay covered |

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

`redact_fuzzer`, `parse_markers_fuzzer`, `pty_login_scan_fuzzer`,
`redact_secret_lines_fuzzer`, `usage_line_to_turn_fuzzer`,
`load_settings_json_obj_fuzzer`, `parse_frontmatter_fuzzer`,
`is_session_not_found_fuzzer` and `pty_screen_feed_fuzzer` ship both (their regexes need
structured tokens the random fuzzer never synthesises); `auth_headers_fuzzer` and
`hosted_redact_obj_fuzzer` ship a dictionary only. The rest grow a corpus fine from
structural input and need neither.

⚠️ **A seed file only reaches the target verbatim if the harness consumes it with
`ConsumeBytes`.** `FuzzedDataProvider.ConsumeUnicodeNoSurrogates` spends the **first byte**
on an internal encoding selector and transforms the rest, so a seed corpus handed to a
harness that uses it arrives with its first character missing and the remainder possibly
re-encoded — every `{`-leading JSON seed becomes `"message":…`, unparseable. Measured on
`usage_line_to_turn_fuzzer`: 26 edges reading its seeds through the unicode consumer
against **65** through `ConsumeBytes(...).decode("utf-8", "replace")`, which is what
`pty_login_scan_fuzzer` already does and why its seed corpus works. Use `ConsumeBytes` for
any harness that ships seeds; the decode with `errors="replace"` also mirrors how these
inputs are really read.

⚠️ **Atheris does not draw every type from the same end of the buffer**, which matters as
soon as a harness consumes more than one thing. Measured on the pinned atheris 3.1.0:

| Call | Draws from |
| --- | --- |
| `ConsumeBytes`, `ConsumeBool`, `ConsumeInt`, `ConsumeFloat` | front |
| `ConsumeIntInRange` | **back** |

Whichever end they come from, a harness that consumes its payload with
`ConsumeBytes(remaining_bytes())` and *then* asks for more values gets the same constant
every time, and whatever those values controlled silently stops being fuzzed — no crash,
no warning, just a dimension that is no longer explored. Reserve bytes for the later draws.
`pty_screen_feed_fuzzer` does, and hit the bug first: its chunk boundaries had degenerated
to a fixed single-byte split.

The effect is not marginal. `pty_login_scan_fuzzer` sits at **11 edges** on random bytes
— it never synthesises `https://`, so it fuzzes only the no-match path — and passes
**200** within the first million iterations once its dictionary and twelve seeds are in
play. Measure before deciding a harness "needs neither": run it locally for a few
hundred thousand iterations and read the `cov:` figure.

⚠️ **Measure at the batch run's real per-harness slice (~37s), not at a million
iterations** — a long enough run lets random bytes stumble into the tokens eventually, so
an iteration-matched comparison understates what seeds and a dictionary buy in CI.
`redact_secret_lines_fuzzer` measured locally: at 1M iterations it is **71 edges** seeded
against **70** bare, i.e. nothing; at 37 seconds it is **71 edges / 432 features** seeded
against **39 / 242** bare — the dict and seeds are worth roughly double the reach inside
the budget the harness actually gets.

If a harness asserts a *property* rather than only "does not crash", prove the
assertion can fail before trusting it. Break the implementation on purpose (monkeypatch
the function under test to return something wrong) and check the harness raises — an
oracle that has never fired is indistinguishable from no oracle at all.
`redact_secret_lines_fuzzer` factors its checks into a `check(text)` the suite drives
directly, so `tests/test_fuzz_harness_smoke.py` can do exactly that on every `just check`
rather than leaving the proof as a one-off a maintainer did once.
