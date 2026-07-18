---
default: minor
---

The config panel's Hooks surface gains a friendly rows editor — add/remove one command hook per row (event, matcher, command, timeout) instead of hand-editing the nested JSON; rows sharing an event + matcher are saved as one group, and raw JSON stays as the escape hatch for shapes the rows can't show. Commands are stored verbatim and never run by Clauster.
