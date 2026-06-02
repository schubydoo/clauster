# Iconoir (vendored, self-hosted)

[Iconoir](https://iconoir.com) — an open-source icon library by Luca Burgio.
MIT licensed (see `LICENSE` in this directory). We self-host (no CDN), the same
as the other vendored front-end assets.

## What's vendored

We do **not** ship the whole icon set or the icon font. We assemble a small SVG
sprite of only the icons the UI uses, inlined once per page at
**`src/clauster/templates/_iconoir_sprite.html`** as `<symbol id="ic-<name>">`
elements, and reference them with `<svg class="ico"><use href="#ic-<name>"/></svg>`.
Inlining (vs. an external `<use href="file.svg#id">`) keeps `currentColor`
theming bulletproof and avoids an extra request.

- **Version:** 7.11.0 (pinned in `../versions.txt`, Renovate-tracked via npm `iconoir`)
- **Source:** `icons/regular/*.svg` from <https://github.com/iconoir-icons/iconoir> at tag `v7.11.0`
- Iconoir's `regular` SVGs already use `stroke="currentColor"`; `stroke-width`
  (1.5) lives on the root `<svg>`, so it is carried onto each `<symbol>`.

## Icons in the sprite

| symbol id   | Iconoir name      | used for                |
|-------------|-------------------|-------------------------|
| `ic-plus`   | `plus`            | New project             |
| `ic-play`   | `play`            | Start bridge            |
| `ic-stop`   | `square`          | Stop bridge             |
| `ic-restart`| `restart`         | Restart bridge          |
| `ic-trust`  | `shield-check`    | Trust directory         |
| `ic-edit`   | `edit-pencil`     | Edit CLAUDE.md          |
| `ic-logs`   | `terminal`        | Show logs               |
| `ic-qr`     | `qr-code`         | QR code                 |
| `ic-copy`   | `copy`            | Copy link               |
| `ic-open`   | `open-new-window` | Open session in Claude  |
| `ic-sun`    | `sun-light`       | Theme toggle (light)    |
| `ic-moon`   | `half-moon`       | Theme toggle (dark)     |
| `ic-warning`| `warning-triangle`| Error / warning states  |
| `ic-trash`  | `bin`             | Reaper delete           |

## Updating

When Renovate bumps `iconoir` in `../versions.txt`, re-run the assembly: fetch
the listed `icons/regular/*.svg` at the new tag, normalize `stroke-width` onto
each `<symbol>`, regenerate `_iconoir_sprite.html`, and refresh this README +
`LICENSE`. To add an icon, add a row above and a `<symbol>` to the sprite.
