---
default: patch
---

ensure_gitignored avoids appending redundant entries when covered by existing .gitignore ancestor directory rules, exact matches, or basename patterns, while respecting subsequent negation rules.
