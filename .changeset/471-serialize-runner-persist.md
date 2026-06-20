---
default: patch
---

Serialize `SessionRunner` state persistence so a concurrent prune-races-upsert window can no longer surface a transient "could not persist bridge state" warning: the persist path now holds its own lock (mirroring the hosted manager), keeping each save atomic against interleaving startup-watch / stop / poll-loop writers.
