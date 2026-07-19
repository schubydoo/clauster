# Support

Thanks for using Clauster! Here's how to get help.

## GitHub Issues is the support channel

GitHub Discussions are intentionally **not** enabled — the
[issue tracker](https://github.com/schubydoo/clauster/issues) is the single place
for questions, bug reports, and feature requests. Before opening one, a quick
[search of existing issues](https://github.com/schubydoo/clauster/issues?q=is%3Aissue)
often turns up an answer or an existing thread to add to.

- **A question or something not working?**
  [Open an issue](https://github.com/schubydoo/clauster/issues/new/choose). Include
  your Clauster version, OS, and the relevant config / log lines. `clauster doctor`
  output is handy for environment problems and safe to share. **Before copying
  bridge log lines, note that the on-disk log is _not_ redacted by default** — the
  redaction you see in the dashboard applies to the live stream only. Set
  `logs.redact_session_url: true` and reproduce if possible, then review the log
  and scrub any remaining session URLs or secrets before sharing it.
- **An idea or enhancement?** Open an issue describing the use case — feature
  requests live in the same tracker.

## Other channels

- **Found a security vulnerability?** Please **don't** open a public issue —
  follow [`SECURITY.md`](https://github.com/schubydoo/clauster/blob/main/SECURITY.md) to report it privately through GitHub's
  security advisories.
- **Want to contribute a fix or feature?** See
  [`CONTRIBUTING.md`](https://github.com/schubydoo/clauster/blob/main/CONTRIBUTING.md) for dev setup and the PR checklist.

Clauster is pre-1.0 and maintained by [@schubydoo](https://github.com/schubydoo)
in spare time, so please be patient — but every report helps. Thanks!
