---
default: minor
---

With an API token (`CLAUSTER_API_TOKEN` or `--token`) and either `tls` or a loopback bind that the same user owns, `clauster doctor` prints the running server's own results, the same rows as the dashboard panel, and its `port` row is OK when this deployment's healthy server holds the port.
