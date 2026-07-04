# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately** through GitHub's
[private vulnerability reporting](https://github.com/schubydoo/clauster/security/advisories/new)
(the "Report a vulnerability" button on the repository's **Security** tab). Do
**not** open a public issue for security reports.

You can expect an initial response within a few days. Once a fix is ready we'll
coordinate disclosure and credit you, if you'd like.

## Supported versions

Clauster is pre-1.0 and under active development; only the latest release on
`main` receives security fixes.

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
