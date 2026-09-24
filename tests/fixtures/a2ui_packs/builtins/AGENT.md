---
id: a2ui-builtins-pack
title: A2UI Builtins Pack
root_expert: root
a2ui_catalogs:
  - clio-workspace
  - basic
blueprint:
  format: agent-blueprint-v1
---

Test-only Agent Blueprint pack for the per-agent A2UI catalog allowlist
(v15 S8): it declares both builtin catalogs by name, workspace first, and
ships no catalog of its own. Tests that produce A2UI surfaces against the
builtins bind their session to this pack, because an agent that declares no
catalogs can produce none.
