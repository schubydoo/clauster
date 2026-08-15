# Bug triage

A lightweight flow so open bugs carry a severity and the backlog is ranked, not flat.
Enhancements, security, and the board-tracking labels are unchanged — this adds one
axis: **how bad is it if we ship without fixing it.**

## Severity rubric

Every `bug` gets exactly one severity label. Read it as *blast radius if left unfixed*,
not *effort to fix*.

| Label | Means | Examples |
| --- | --- | --- |
| **severity:high** | Correctness or safety: the system is **silently wrong**, loses data, lies about its own state, or leaks a secret. A caller/operator acts on a wrong-but-plausible answer. | reports success while doing nothing; a floating release tag moves backwards; a persisted safety setting is silently dropped; a credential reaches a subprocess argv. |
| **severity:medium** | A real functional bug, but with limited blast radius, a visible failure, or a workaround. | a stuck/stranded UI row; metrics attributed to the wrong session; a log that can't be read though the file is on disk; an accessibility gap. |
| **severity:low** | Cosmetic, a narrow edge case, or dev-only. | duplicated form control; a diagnostic false-negative; test-suite process leakage on the host. |

Tie-breaker: if a bug **misleads the person reading it** (operator or agent), lean
higher — a launchpad that lies about state erodes trust fastest.

Two existing labels compose with severity, they don't replace it:

- **`needs-repro`** — severity is the *stakes*; `needs-repro` is *confidence*. A
  high-stakes bug still needs a deterministic repro before a fix lands.
- **`security`** — keep it; add `severity:high` too so it sorts into the burn-down.

## Ranked backlog

The ranked backlog is just the issue list filtered and sorted by severity — no
separate spreadsheet to drift:

```sh
gh issue list --label bug --state open --label severity:high
gh issue list --label bug --state open --label severity:medium
gh issue list --label bug --state open --label severity:low
```

Work `severity:high` to empty first, then `severity:medium`. Within a tier, prefer the
bugs that mislead a caller over those that merely inconvenience one.

## Fixing

- **One PR per bug**, or a small, tightly-scoped batch of the same class (e.g. one
  cluster of keeper-liveness edges).
- **Ship a regression test** with every fix where feasible — a test that fails on the
  bug and passes on the fix. If a deterministic test isn't feasible, say why in the PR.
- **Escalate product calls.** If a bug is really a *"should we support this?"* decision
  (not a defect), don't guess — raise it with the maintainer and label it accordingly
  rather than closing it as fixed.

## Applying the labels

Labels are managed as code in [`.github/repo-config/labels.json`](repo-config/labels.json).
A maintainer runs the `repo-config-apply` workflow (dry-run first) to create the three
`severity:*` labels on the repo; the `repo-config-drift` check is advisory and reports
if the live set diverges from the baseline. Once the labels exist, apply them across the
open `bug` backlog per the rubric above.
