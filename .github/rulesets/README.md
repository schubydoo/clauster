# Repository rulesets

`main.json` is the source-of-truth for the **`main` branch ruleset** — the
replacement for the legacy branch-protection rules previously declared in
`.github/settings.yml` (the [`repository-settings`][app] Probot app, now
deprecated and removed).

It encodes the same contract the project has always enforced:

- required status checks (`ci required checks passed`, `security required checks
  passed`, `conventional PR title`, `lint`) with strict / up-to-date branches,
- linear history, conversation resolution, a pull request required (0 approvals —
  solo maintainer), and blocked deletions / force-pushes,
- `enforcement: active` with an empty `bypass_actors` list — i.e. admins are
  enforced too (the old `enforce_admins: true`).

## Applying

The ruleset is currently applied/maintained manually via the API:

```sh
# create (first time)
gh api -X POST repos/<owner>/<repo>/rulesets --input .github/rulesets/main.json

# update an existing ruleset (look up its id first)
id=$(gh api repos/<owner>/<repo>/rulesets --jq '.[]|select(.name=="main")|.id')
gh api -X PUT repos/<owner>/<repo>/rulesets/"$id" --input .github/rulesets/main.json

# verify the effective rules on the branch
gh api repos/<owner>/<repo>/rules/branches/main
```

A small GitHub Action to reconcile this file (plus repo settings and labels)
automatically — applying on merge and re-applying on a schedule to revert
out-of-band drift — is planned. Until then, edit `main.json` and re-apply by hand.

[app]: https://github.com/repository-settings/app
