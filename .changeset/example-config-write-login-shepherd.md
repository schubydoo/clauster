---
default: patch
---

`clauster.yml.example` again shows the `config_write` and `login_shepherd` sections (with `allow_user_scope` / `allow_setup_token`) as commented-out defaults with a one-line explanation and a pointer to the config reference (#1012). The #983 declutter had dropped them, so a new operator starting from the example couldn't discover the config-management panel or the login shepherd — the surfaces the 1.0 test-plan pre-flight tells testers to enable.
