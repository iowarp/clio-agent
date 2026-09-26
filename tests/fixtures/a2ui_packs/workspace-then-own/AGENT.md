---
id: a2ui-workspace-then-own-pack
title: A2UI Workspace-Then-Own Pack
root_expert: root
a2ui_catalogs:
  - clio-workspace
  - own: catalogs/own
blueprint:
  format: agent-blueprint-v1
---

Test-only Agent Blueprint pack for the per-agent A2UI catalog allowlist
(v15 S8). It declares the `clio-workspace` builtin first and then a catalog it
ships itself (`own`): the same shape the shipped `earthscope-single-agent` pack
declares, kept in-repo so the allowlist tests do not depend on the marketplace
submodule.
