---
default: minor
---

Completes the config-change audit trail's subprocess-visibility slice (#958 Part 6): MCP **and plugin** writes now record the redacted `claude mcp`/`claude plugin` **argv** the CLI actually ran, and the before/after **file fingerprints** (path + SHA-256 + size, never contents) now cover the reset-project-choices, approvals, plugin, and marketplace handlers too — not just MCP-server writes. So `config_audit.log` answers both "where did this change land?" and "what ran?" for every CLI-driven config mutation.
