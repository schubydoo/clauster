# Vendored third-party assets — Tabler

**Package:** [`@tabler/core`](https://www.npmjs.com/package/@tabler/core) **1.4.0** · MIT.

These are Tabler's prebuilt `dist/` artifacts, self-hosted so Clauster never reaches
a CDN (keeps the loopback / no-telemetry posture).

| File | Source |
|------|--------|
| `css/tabler.min.css` | `@tabler/core@1.4.0` npm tarball → `package/dist/css/tabler.min.css` |
| `js/tabler.min.js`   | `@tabler/core@1.4.0` npm tarball → `package/dist/js/tabler.min.js` |
| `LICENSE`            | MIT, copied verbatim from `tabler/tabler` `main/LICENSE` — the `@tabler/core` npm subpackage omits the file, but declares `"license": "MIT"` and carries the MIT banner inline in both dist files |

Provenance verified: the dist files were confirmed **byte-identical (sha256)** between
the npm tarball (`npm pack @tabler/core@1.4.0`) and the jsDelivr CDN copies. The MIT
banner (© The Tabler Authors / Paweł Kuna) is preserved inline at the top of each
minified file. Tabler Core's CSS has **no external `url()` references** (no fonts or
images to vendor); UI icons are inline SVG, so `@tabler/icons` is not a dependency.

## Updating

```sh
npm pack @tabler/core@<version>
tar xzf tabler-core-<version>.tgz
cp package/dist/css/tabler.min.css css/tabler.min.css
cp package/dist/js/tabler.min.js   js/tabler.min.js
# LICENSE: refresh from https://raw.githubusercontent.com/tabler/tabler/v<version>/LICENSE
```

Then bump the version above and re-test the dashboard.
