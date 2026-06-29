# Vendored third-party assets — xterm.js

**Package:** [`@xterm/xterm`](https://www.npmjs.com/package/@xterm/xterm) **6.0.0** · MIT.

These are xterm.js's prebuilt dist artifacts, self-hosted so Clauster never reaches a
CDN (keeps the loopback / no-telemetry posture, and `script-src`/`connect-src` stay
`'self'`). Used by the read-only live pty terminal view (#534).

| File | Source |
|------|--------|
| `js/xterm.js`  | `@xterm/xterm@6.0.0` npm tarball → `package/lib/xterm.js` (the minified UMD build exposing the `Terminal` global) |
| `css/xterm.css`| `@xterm/xterm@6.0.0` npm tarball → `package/css/xterm.css` |
| `LICENSE`      | MIT, copied verbatim from the `@xterm/xterm@6.0.0` npm tarball → `package/LICENSE` |

We vendor the **UMD** build (`lib/xterm.js`), not the ESM `lib/xterm.mjs`, so it loads
via a plain `<script>` tag (no module graph). The file is shipped pre-minified by
upstream; the MIT banner is in `LICENSE` alongside it. xterm.js core has no external
`url()`/font references in `xterm.css`; addons (fit, search, …) are **not** vendored —
the live view uses the core `Terminal` only.

## Local patch — CSP nonce on injected `<style>` (#635)

`js/xterm.js` is **patched, not pristine.** xterm's DOM renderer creates `<style>`
elements at runtime (`createElement("style")`) and never sets a nonce. Under
Clauster's strict `style-src 'self' 'nonce-…'` (no `'unsafe-inline'`, #635) every
nonce-less injected `<style>` is blocked, so the terminal's dimensions grid, theme,
and base styles never apply and the live view renders mangled.

The fix stamps the per-request nonce on each injected `<style>` **before it is
inserted** (a MutationObserver would be too late — CSP blocks at insertion). At each
of the **4** `createElement("style")` sites we set both `el.nonce` and
`el.setAttribute("nonce", …)` from the `window.__clausterCspNonce` global, which the
dashboard template emits from a nonce'd inline `<script>` (`templates/dashboard.html`).

**Re-apply this on every xterm bump.** After copying a fresh `js/xterm.js`:

```sh
grep -c 'createElement("style")' js/xterm.js   # expect 4 sites
```

For each site, immediately after the created element, insert (using that site's
element variable, e.g. `el`):

```js
el.nonce=window.__clausterCspNonce||"",el.setAttribute&&el.setAttribute("nonce",window.__clausterCspNonce||""),
```

then confirm `grep -c '__clausterCspNonce' js/xterm.js` is `8` (2 per site) and
`node --check js/xterm.js` passes. Do **not** reformat the minified bundle.

## Updating

```sh
npm pack @xterm/xterm@<version>
tar xzf xterm-xterm-<version>.tgz
cp package/lib/xterm.js   js/xterm.js
cp package/css/xterm.css  css/xterm.css
cp package/LICENSE        LICENSE
```

Then bump the version in `../versions.txt`, **re-apply the CSP-nonce patch above**,
and re-test the live terminal view.
