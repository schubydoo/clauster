# Repository rulesets

`main.json` is the source-of-truth for the **`main` branch ruleset** — the
replacement for the legacy branch-protection rules previously declared in
`.github/settings.yml` and reconciled by the [`repository-settings`][app] Probot
app. That config file is removed in this PR, and the app itself has been
uninstalled from the repository — the ruleset is now the sole source of branch
protection.

It encodes the same contract the project has always enforced:

- required status checks (`ci required checks passed`, `security required checks
  passed`, `conventional PR title`, `lint`, `jactionlint`) with strict / up-to-date
  branches. `.github/jactionlint.yaml` lists the same checks under `required-checks`,
  so keep the two lists equal,
- linear history, conversation resolution, a pull request required (0 approvals —
  solo maintainer), and blocked deletions / force-pushes,
- `enforcement: active` with an empty `bypass_actors` list — i.e. admins are
  enforced too (the old `enforce_admins: true`).

## Tag protection + immutable releases

`tags.json` is a second ruleset (`target: tag`) over `refs/tags/v*` that blocks
**deletion** and **non-fast-forward** (moving) of release tags, with no bypass —
so a published version tag can't be repointed or removed. Tag *creation* is not
blocked, so knope can still cut new `vX.Y.Z` tags at release time.

This pairs with **immutable releases** (enabled on the repo via
`PUT /repos/{owner}/{repo}/immutable-releases`): once a release is published its
tag is locked to its commit and its assets can't be changed, and a release
attestation is generated. Immutability applies to *future* releases only; the
release **body/notes remain editable**, so the post-publish
`gh release edit --notes-file` step (when needed) still works.

## Applying

The rulesets are currently applied/maintained manually via the API. Each file
maps to one ruleset: `main.json` → ruleset **`main`** (branch), `tags.json` →
ruleset **`protect-version-tags`** (tag).

```sh
# create (first time)
gh api -X POST repos/<owner>/<repo>/rulesets --input .github/rulesets/main.json
gh api -X POST repos/<owner>/<repo>/rulesets --input .github/rulesets/tags.json

# update an existing ruleset (look up its id by name first)
id=$(gh api repos/<owner>/<repo>/rulesets --jq '.[]|select(.name=="main")|.id')
gh api -X PUT repos/<owner>/<repo>/rulesets/"$id" --input .github/rulesets/main.json
id=$(gh api repos/<owner>/<repo>/rulesets --jq '.[]|select(.name=="protect-version-tags")|.id')
gh api -X PUT repos/<owner>/<repo>/rulesets/"$id" --input .github/rulesets/tags.json

# verify: effective branch rules, every ruleset, and immutable releases
gh api repos/<owner>/<repo>/rules/branches/main
gh api repos/<owner>/<repo>/rulesets --jq '.[]|{id,name,target,enforcement}'
gh api repos/<owner>/<repo>/immutable-releases   # expect {"enabled": true, ...}
```

Immutable releases is a repo setting, not a ruleset file: enable with
`gh api -X PUT repos/<owner>/<repo>/immutable-releases` (disable with `-X DELETE`).

### Compare the file with the live ruleset before a PUT

A PUT replaces the whole ruleset with the file. If the file leaves out a parameter
that the live ruleset has, the PUT can set that parameter back to its default. For
example, until November 2026 the file did not have
`require_extra_approval_for_unattributed_changes` or `required_reviewers`, which the
live `main` ruleset had.

Before each PUT, compare the file with the live ruleset. The commands only read. Run
them in bash or zsh (the `<(...)` form is not POSIX `sh`), with `jq` on your `PATH`:

```bash
name=main file=.github/rulesets/main.json   # or: protect-version-tags, tags.json
keys='{name,target,enforcement,bypass_actors,conditions,rules}'
id=$(gh api repos/<owner>/<repo>/rulesets --jq ".[]|select(.name==\"$name\")|.id")
gh api repos/<owner>/<repo>/rulesets/"$id" | jq -S "$keys" \
  | diff <(jq -S "$keys" "$file") -
```

In the output, `<` lines come from the file and `>` lines come from the live ruleset.
The output must show only the values that you edited. `jq -S` sorts object keys but
not array items, so a change in array order also shows as a difference. If the output
shows a value that you did not edit, the live ruleset and the file do not agree on it.
Copy the live value into the file first, in its own pull request. If the live value is
an out-of-band change that you want to undo, the PUT is the fix.

A small GitHub Action to reconcile these files (plus repo settings and labels)
automatically — applying on merge and re-applying on a schedule to revert
out-of-band drift — is planned. Until then, edit the JSON and re-apply by hand.

[app]: https://github.com/repository-settings/app
