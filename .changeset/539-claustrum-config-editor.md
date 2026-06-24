---
default: patch
---

Config editor: the **Claustrum (hosted live-view)** block is now editable in-app (#539).
Previously `claustrum.enabled` could only be flipped by hand-editing `clauster.yml` and
restarting, so a user with claustrum installed had no discoverable way to turn the hosted
channel on. The editor now surfaces the `claustrum.enabled` master toggle plus its
operational fields (`socket_path`, `spawn_timeout_seconds`, `keep_children`,
`request_timeout_seconds`), which grey out when the channel is off (same depends-on
mechanism as the metrics block). Like every config edit, saving prompts to restart Clauster
to apply. `claustrum.binary` is intentionally left out — executable paths stay non-editable,
matching `claude.binary`.
