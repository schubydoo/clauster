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

## Updating

```sh
npm pack @xterm/xterm@<version>
tar xzf xterm-xterm-<version>.tgz
cp package/lib/xterm.js   js/xterm.js
cp package/css/xterm.css  css/xterm.css
cp package/LICENSE        LICENSE
```

Then bump the version in `../versions.txt` and re-test the live terminal view.
