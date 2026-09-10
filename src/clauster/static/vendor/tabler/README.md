# Vendored third-party assets — Tabler

**Package:** [`@tabler/core`](https://www.npmjs.com/package/@tabler/core) **1.5.1** · MIT.

These are Tabler's prebuilt `dist/` artifacts, self-hosted so Clauster never reaches
a CDN (keeps the loopback / no-telemetry posture).

| File | Source |
|------|--------|
| `css/tabler.min.css` | `@tabler/core@1.5.1` npm tarball → `package/dist/css/tabler.min.css` |
| `js/tabler.min.js`   | `@tabler/core@1.5.1` npm tarball → `package/dist/js/tabler.min.js` |
| `LICENSE`            | MIT, copied verbatim from `tabler/tabler` at tag `@tabler/core@1.5.1`. The `@tabler/core` npm subpackage omits the file. It declares `"license": "MIT"` and carries the MIT banner inline in both dist files. |

Provenance verified: the dist files are byte-identical (sha256) between the npm tarball
(`npm pack @tabler/core@1.5.1`) and the jsDelivr CDN copies. The MIT banner
(© The Tabler Authors / Paweł Kuna) is preserved inline at the top of each minified file. Tabler Core's CSS still has **no external `url()` references** (verified: no
fonts or images to vendor). UI icons are inline SVG, so `@tabler/icons` is not a dependency.
As of 1.5, Tabler bundles Bootstrap 5.3.8 in its own source tree, so no separate Bootstrap
asset is needed.

## Updating

```sh
npm pack @tabler/core@<version>
tar xzf tabler-core-<version>.tgz
cp package/dist/css/tabler.min.css css/tabler.min.css
cp package/dist/js/tabler.min.js   js/tabler.min.js
# LICENSE: refresh from https://raw.githubusercontent.com/tabler/tabler/@tabler/core@<version>/LICENSE
# Then verify the sha256 of both dist files against the jsDelivr copy (see Provenance above).
```

Then update the version in this file, in `../versions.txt`, and in `THIRD_PARTY_NOTICES.md`
(the version line plus the copyright line). Re-test the dashboard.
