# Repo config baselines (advisory drift check)

Declarative baselines for the repository's **labels** and a few **basic
settings**, plus an advisory CI job that warns when the live GitHub config
drifts from what is committed here.

- `labels.json` — every label's `name`, `color`, and `description`. The file's
  on-disk order/formatting is not significant: the drift check normalises both
  the committed file and the live list (sort by name, sort keys) before
  comparing, so either compact or pretty JSON works.
- `settings.json` — `description`, `homepage`, `topics`, `has_issues`,
  `has_wiki`, `has_projects`, the three `allow_*_merge` flags, and
  `delete_branch_on_merge`. **Token split:** `GET /repos` returns the merge flags
  (and `delete_branch_on_merge`) only to an admin-scope token, which the drift
  check's default `GITHUB_TOKEN` lacks. So the PR-facing `drift` job (no secrets, fork-safe)
  verifies labels + the read-visible settings, and a separate `settings-admin` job
  verifies the merge flags with `REPO_CONFIG_TOKEN` — running **only on non-PR events**
  (push to `main` / schedule / dispatch), so no secret is ever exposed to a PR.

## What runs

[`.github/workflows/repo-config-drift.yml`](../workflows/repo-config-drift.yml)
re-fetches the live label list and repo settings with `gh api` GETs, normalises
both sides (labels sorted by name, object keys sorted), diffs them against the
JSON here, and prints any drift to the workflow's job summary.

## It is READ-ONLY and ADVISORY

- **Read-only.** The workflow only performs `gh api` GETs with the default
  `GITHUB_TOKEN`. It uses no secrets and writes nothing — not labels, not
  settings, not branch protection or rulesets (those are owned by the repo
  ruleset and are intentionally out of scope).
- **Fork-safe.** Plain `pull_request` trigger (never `pull_request_target`) with
  `permissions: contents: read` (+ `issues: read` for the label API). A fork PR
  cannot exfiltrate or mutate anything.
- **Non-blocking.** The job always exits `0` (drift only prints a diff) and is
  additionally wrapped in `continue-on-error`. **Do not add it to the repo's
  required status checks** — it must never gate a merge.

## Applying the baseline (reconcile)

The **apply / reconcile** half — writing labels and settings back to match these
baselines — is
[`.github/workflows/repo-config-apply.yml`](../workflows/repo-config-apply.yml).
It is **maintainer-run only** and fail-safe:

- **`workflow_dispatch` only** (never `pull_request`/`push`) — a write workflow must
  not be reachable from an automatic or untrusted trigger.
- **`dry_run` defaults true** — it prints the plan to the job summary and writes
  nothing until you uncheck it.
- **`prune` defaults false** — it does not delete labels missing from the baseline
  unless you opt in (a delete strips the label from every issue/PR).
- Writes use a **`REPO_CONFIG_TOKEN`** secret — a scoped GitHub App token or
  fine-grained PAT with **Administration: write** + **Issues: write**. The default
  `GITHUB_TOKEN` cannot `PATCH` repo settings, so an apply with no secret fails
  closed. Branch protection / rulesets stay out of scope (owned by the ruleset).

## Updating the baseline

When the live config legitimately changes (a new label, an edited description, a
new topic), accept it as the new baseline by regenerating the JSON from your
local machine:

```sh
gh label list --repo schubydoo/clauster --json name,color,description \
  | jq -S 'sort_by(.name)' > .github/repo-config/labels.json

gh api repos/schubydoo/clauster --jq '{
  description, homepage, topics: (.topics | sort),
  has_issues, has_wiki, has_projects,
  allow_squash_merge, allow_merge_commit, allow_rebase_merge,
  delete_branch_on_merge
}' | jq -S '.' > .github/repo-config/settings.json
```

Commit the result in a normal PR. After it merges the drift check is green
again.
