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

Clauster spawns and manages `claude remote-control` bridges on the host it runs
on — it is **trusted, host-local infrastructure**, not a multi-tenant service.
Key considerations:

- Loopback-only by default; binding to a network interface requires auth
  (password login or a trusted reverse proxy) — see the README.
- Starting a bridge, editing a project's `CLAUDE.md`, or cloning a repository
  runs code from the target directory on the host. Treat `projects_root` as
  trusted.
- The clone and ghost-environment-reaper features reach the network / first-party
  APIs with the host's own credentials; they are gated (SSRF guards, typed
  confirmations, opt-in flags) but act on the operator's behalf.

Reports that require already having shell/host access, or that amount to "the
operator can manage their own host," are generally out of scope.
