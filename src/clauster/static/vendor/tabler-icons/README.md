# Tabler Icons (vendored, self-hosted)

[Tabler Icons](https://tabler.io/icons) — an open-source icon set by Paweł Kuna.
MIT licensed (see `LICENSE` in this directory). It's the same family as the Tabler
CSS kit we use, so the icons match the UI by design. We self-host (no CDN), the same
as the other vendored front-end assets.

## What's vendored

We do **not** ship the whole icon set or the icon webfont. We assemble a small SVG
sprite of only the icons the UI uses, inlined once per page at
**`src/clauster/templates/_tabler_sprite.html`** as `<symbol id="ic-<name>">`
elements, and reference them with `<svg class="ico"><use href="#ic-<name>"/></svg>`.
Inlining (vs. an external `<use href="file.svg#id">`) keeps `currentColor`
theming bulletproof and avoids an extra request.

- **Version:** 3.45.0 (pinned in `../versions.txt`, Renovate-tracked via npm `@tabler/icons`)
- **Source:** `icons/outline/*.svg` from <https://github.com/tabler/tabler-icons> at tag `v3.45.0`
- Tabler's `outline` SVGs already use `stroke="currentColor"`; the stroke **weight**
  comes from the `.ico` CSS class (1.5), not the symbol. Each `<symbol>` records its
  Tabler icon name in a `<!-- ti <name> -->` comment.

## Adding / updating icons

- **Add one:** append a `<symbol id="ic-<id>" …><!-- ti <name> -->…paths…</symbol>` to
  the sprite, copying the inner paths of the outline SVG (drop the leading
  `M0 0h24v24H0z` background path) from <https://tabler.io/icons> or
  `node_modules/@tabler/icons/icons/outline/<name>.svg`.
- **Bump version:** Renovate updates `../versions.txt`; on merge, re-vendor `LICENSE`,
  refresh any changed icon paths in the sprite, and update the version here + in the
  repo's `THIRD_PARTY_NOTICES.md`.
