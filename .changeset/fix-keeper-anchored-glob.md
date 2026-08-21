---
default: patch
---

`clauster keepers` no longer hides (and `--kill` no longer refuses) a removed project's live keeper when a carded sibling shares its name prefix — e.g. a carded `app` used to swallow `app-staging`'s orphaned keeper. Protection now keys on the exact parsed `<name>` stem instead of an unanchored `<project>-*` glob. (#1181)
