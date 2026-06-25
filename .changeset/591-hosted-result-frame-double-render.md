---
default: patch
---

Fix the hosted live-view rendering each assistant reply twice. One assistant turn emits both the streamed `assistant` frame and a trailing `result` frame whose `result` field repeats the same text, and the live-view rendered both — the message as white paragraphs and the result echo as a green run-on block. The `result` frame now collapses a successful turn to a "turn complete" marker and surfaces text only on the error path (where `result` carries content no assistant frame emits, e.g. "Not logged in · Please run /login").
