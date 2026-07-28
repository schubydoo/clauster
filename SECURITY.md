# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately** through GitHub's
[private vulnerability reporting](https://github.com/schubydoo/clauster/security/advisories/new)
(the "Report a vulnerability" button on the repository's **Security** tab). Do
**not** open a public issue for security reports.

You can expect an initial response within a few days. Once a fix is ready we'll
coordinate disclosure and credit you, if you'd like.

## Supported versions

Clauster is actively developed; only the latest release receives security
fixes.

## Scope & threat model

Clauster spawns and manages `claude` bridges and agent sessions (the standard
and pty `remote-control` bridges, `claude --bg` background agents, and the hosted
claustrum channel) on the host it runs on — it is **trusted, host-local
infrastructure**, not a multi-tenant service. Key considerations:

- Loopback-only by default; binding to a network interface requires auth
  (password login or a trusted reverse proxy) — see the
  [networking guide](https://schubydoo.github.io/clauster/networking/) for the
  full loopback / non-loopback auth matrix.
- Starting a bridge, editing a project's `CLAUDE.md`, or cloning a repository
  runs code from the target directory on the host. Treat `projects_root` as
  trusted.
- The clone and ghost-environment-reaper features reach the network / first-party
  APIs with the host's own credentials; they are gated (SSRF guards, typed
  confirmations, opt-in flags) but act on the operator's behalf.
- Release artifacts (the GHCR image and the standalone binaries) are
  Sigstore-signed; verify them before running — see the
  [installation guide](https://schubydoo.github.io/clauster/installation/) for the
  `cosign` / `gh attestation verify` flow.

Reports that require already having shell/host access, or that amount to "the
operator can manage their own host," are generally out of scope.

## Supply-chain and CI controls

The threat model above is backed by controls you can inspect in
[`.github/workflows/`](https://github.com/schubydoo/clauster/tree/main/.github/workflows):

- **Static + dependency analysis on every PR** — CodeQL, OSV-Scanner, and
  GitHub dependency review; secret scanning and Trivy image scanning run in the
  security workflow.
- **Continuous fuzzing** — ClusterFuzzLite runs the Atheris harnesses in
  [`fuzz/`](https://github.com/schubydoo/clauster/tree/main/fuzz) on PRs and on
  a schedule.
- **Pinned everything** — every GitHub Action is pinned to a full commit SHA
  (enforced transitively into reusable workflows), and Python dependencies
  resolve from the committed `uv.lock`.
- **Keyless, token-free releases** — PyPI publishes via Trusted Publishing
  (OIDC; no long-lived API token exists to steal), and every binary and
  container image is Sigstore-signed with SLSA provenance — the
  [installation guide](https://schubydoo.github.io/clauster/installation/)
  documents the `cosign` / `gh attestation verify` flow.
- **Public scorecards** — OpenSSF Scorecard and Best Practices are tracked
  continuously (badges in the README's Project health section).

## Past advisories

- [GHSA-h4g2-xfmw-q2c9](https://github.com/schubydoo/clauster/security/advisories/GHSA-h4g2-xfmw-q2c9)
  (2026-06) — non-loopback deployments could serve the dashboard
  unauthenticated when `auth.enabled` was unset. Fixed fail-closed in 0.2.2; a
  non-loopback bind now refuses to start without enforced auth.
