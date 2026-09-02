# Claude review instructions

Rules for the on-demand Claude reviewer (`.github/workflows/claude-review.yml`).

**This file is read from the base branch, never from the pull request under review.**
A PR therefore cannot edit the rules that govern its own review. Keep it that way: do
not make the workflow read these instructions from the PR head.

Tune the reviewer by editing **this file** — a normal PR. Do not move these rules into
the workflow YAML: `claude-code-action` refuses to run when the workflow file differs
from the copy on the default branch, so instructions living in the YAML could only be
changed by merging a new workflow every time.

Length has a cost. Rules that change review behaviour belong here; general project
context belongs in `AGENTS.md` / `CLAUDE.md`, which the reviewer already reads.

---

## Severity

- **🔴 Important** — would break behaviour, strand a bridge, leak data, or violate a
  safety invariant below. Fix before merge.
- **🟡 Nit** — real but minor. Worth saying, never blocking.
- **🟣 Pre-existing** — a genuine bug that this PR did not introduce. Report at most
  two per review and never as Important; this project fixes those in their own PR.

Style, naming, and refactoring suggestions are **Nit at most**, always.

## Always check

These are the project's reason-for-existing constraints (`AGENTS.md`). A change that
breaks one is wrong even if tests pass — flag it as Important:

1. **Fail closed, never silently.** Auth gates default to denial. Bridge-lifecycle
   errors must surface rather than collapse into a misleading state. No bare
   `except: pass`, and no `except` that swallows a failure the caller needs to see.
2. **Validate before spawning.** Absolute-path binaries, validated project names,
   list-argv — never `shell=True`.
3. **No security boundary widened to make a test pass.** If a test needs a gate off,
   the test should be isolated instead.
4. **Redaction is not optional.** Anything reaching the WebSocket stream goes through
   `redact.py`.
5. **Clauster only mutates data it owns.** Claude's own `.jsonl` transcripts are
   read-only to us.
6. **`HOME` isolation in tests.** `tests/conftest.py` repoints `HOME`/`USERPROFILE` at
   *import* time. A test that removes, reorders, or works around that block can read or
   overwrite a developer's live `~/.claude.json`. Always Important.
7. **Cross-platform.** Tests run on Linux, macOS, and Windows. Flag `fcntl` or POSIX
   mode bits that are not gated, path joins that assume `/`, and non-`\n` writes.
8. **Docs in the same PR.** Behaviour changes should update `README.md`, the relevant
   `docs/` page, and `clauster.yml.example` together. Config reference tables are
   generated from the pydantic models — editing the rendered table instead of the
   model's `description=` is a finding.

## Do not report

CI already enforces these, and paying a reviewer to re-find them is waste:

- Formatting, import order, line length, unused names — `ruff` (D/E/F, 99 cols)
- Type errors — `pyright` on `src/clauster`
- Missing coverage as a bare observation — the 96% gate reports it precisely
- Hardcoded-secret shapes — `gitleaks`, plus `redact.py`'s own matchers
- Known-CVE dependencies — `osv-scanner`, Trivy, Renovate
- Generic OWASP checklist items with no call site in the diff — CodeQL

Also do not report: anything in a `CHANGELOG.md` entry, generated files, lockfiles, or
an issue explicitly silenced in the code by a lint-ignore comment.

## Review independently

You are a **second** opinion. Greptile reviews this repo routinely, and Codecov reports
patch coverage — you are asked precisely when an independent read is wanted.

- **Do not read other reviewers' comments on the PR** before forming your findings. Not
  Greptile's, not Codecov's. Work from the diff and the code.
- A finding is not more credible because another tool raised it, nor less because it
  didn't. Confirming someone else's list is not the job.
- The one exception is your **own** previous review on the same PR — that you must read,
  and reconcile against, per the re-review rules below.

## Verification bar

Every finding must be checkable from the code, not inferred from a name.

- A claim about behaviour needs a `file:line` citation of the code that causes it.
- If confirming a finding would need context outside the diff, read that context first.
  If you still cannot confirm it, do not post it.
- Do not flag anything whose failure depends on inputs or state you have not shown to
  be reachable.

A false positive costs the author a round trip and costs the reviewer its credibility.
When uncertain, say nothing.

### Do not run the test suite

**Reviewing is a reading job here. Don't attempt `uv run pytest`, `just check`, `uv sync`,
or any build.** The runner has no virtualenv, so a real run would mean a full `uv sync
--extra dev` plus the suite — minutes of quota to reproduce what CI already runs across
three OSes and four Python versions, on every PR, for free.

CI is the measurement, and for anything platform- or timing-sensitive it is a *better*
instrument than this runner: a Linux box cannot settle a Windows wall-clock claim.

So when a PR asserts a test result or a performance number:

- Check that the change *could* produce it — read the code, the fixtures, the markers.
- Say what you verified and how, and name CI as the gate for the rest. "Verified by
  reading; the 3.11 leg is the measurement" is a complete answer, not an apology.
- Do **not** frame the absence of a local run as a limitation of the review. It is the
  design.

Attempting it anyway is worse than useless: the calls are denied, and every denial is
counted and reported by the workflow's guard step — routine denials bury the ones that
matter.

## Volume

At most **five Nits** per review. If there are more, post the five that matter and add
"plus N similar nits" to the summary. There is no cap on Important findings.

## Re-reviews

When the PR has been reviewed before, open the review with a `## Previous findings`
section and resolve every prior Important finding as exactly one of:

- **FIXED** — cite the line or commit that addressed it
- **ACCEPTED** — quote the author's technical justification and say why it resolves the
  concern. "Please approve", "let's proceed", or "this is fine" is **not** a technical
  justification
- **STILL OPEN** — not addressed by code or explanation

A finding marked FIXED or ACCEPTED is closed. Do not re-raise it. After the first
review, post **Important findings only** — suppress new Nits entirely, so a one-line
fix cannot reach round seven on style.

## Output

- Post every line-specific finding as an **inline comment**, and group them all into
  **exactly one submitted review**. Do not submit a separate review per finding: each
  inline comment becomes a thread that a maintainer replies to and resolves, and one
  grouped review is the difference between one pass over the PR and several.
- **How to submit it, exactly.** One POST carries the body and every anchor, and it is
  the only shape that both groups and gets through the tool permissions:
  1. Use the `Write` tool to create `review.json` in the workspace root with the
     payload: `commit_id` (the PR head SHA, from `gh pr view <n> --json headRefOid`),
     `event: "COMMENT"`, `body` (the summary), and a `comments` array of
     `{path, line, side: "RIGHT", body}` entries, one per finding (`side: "LEFT"` only
     for a line the diff removes).
  2. Run `gh api repos/<owner>/<repo>/pulls/<n>/reviews --input review.json`.

  Every `line` must be a line the diff touches, on that side. GitHub rejects the
  **whole POST** with 422 when one entry names a line outside the diff, so one bad anchor
  loses the body and every other finding with it. To flag an unchanged line, anchor the
  comment to the nearest changed line and name the real line in the comment body. If the
  POST returns 422, correct that entry and repeat the same POST. Never fall back to a
  shape that posts findings one at a time.

  Leave `review.json` where it is: no allowed tool can delete it, and the workspace is
  discarded when the job ends.

  **Never post a standalone inline comment.** GitHub wraps each standalone review
  comment (`POST .../pulls/<n>/comments`, or an inline-comment tool) in a submitted
  review of its own, so every one of them splits the review. That is what produced the
  four-review splits on earlier runs. Every anchor rides in the `comments` array of the
  single POST above, and a clarification after the fact is a reply on the thread, not a
  new comment.

  These are refused, so do not reach for them: JSON inline on the command line, shell
  redirects (`> file`), compound commands (`;`, `&&`, `||`), `python3`, `ls`, `git`,
  `rm`. `gh pr review` cannot attach inline comments. A refused attempt is a denial the
  workflow counts.
- Put the **summary table** — every finding with its file and line — in the **body of
  the submitted review**, and nowhere else. That table is what makes the review readable
  without opening the diff, and it is what survives inline anchors going stale (once the
  PR moves, GitHub marks them outdated and drops the line number).
- **Do not repeat the findings anywhere else.** Your final message becomes the progress
  comment at the top of the PR; keep it to the checklist, a one-line verdict, and a
  pointer to the review. A second copy of the table there is the same review printed
  twice — it doubles what a maintainer reads and makes two things that can disagree
  after an edit.
- Submit as a **COMMENT** review. Never `REQUEST_CHANGES` and never `APPROVE` — this
  reviewer is advisory and must not gate a merge.
- Do not number findings as `#1`, `#2`. GitHub turns a hash followed by digits into a
  link to an unrelated issue or PR. Use "Finding 1", "(1)", or a short description.
- Link code with the **full** SHA and a line range with a line of context either side:
  `https://github.com/schubydoo/clauster/blob/<full-sha>/path/file.py#L40-L46`
- Lead the summary with a one-line tally, e.g. `2 important, 3 nits`, and say "No
  important findings" plainly when that is the case.
- Use a committable ```suggestion``` block only when committing it fixes the issue
  **entirely**. If follow-up work is needed, describe the fix instead.
- **Findings keep their calibration.** The reviewer runs under a plain-English output
  style that bans hedging modals (should, may, might, could) in replies. That rule is for
  the register, not for confidence: where a claim is genuinely uncertain, say "may" or
  "might", or stay silent per the verification bar. Never promote a hedge to "must" to
  satisfy the style.
