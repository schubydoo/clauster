---
default: patch
---

Fix Stopped session cards vanishing when an unrelated `claude` happened to be running in the same project folder, and stale phantom cards never being cleaned up while another session on that project was live: the dashboard's phantom-prune now decides per instance rather than per project, and only treats an external session as a bridge when its command line actually is one (including the `--rc` form, which it previously failed to recognise).
