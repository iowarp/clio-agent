---
id: a2ui-workspace-only-pack
title: A2UI Workspace-Only Pack
root_expert: root
a2ui_catalogs:
  - clio-workspace
blueprint:
  format: agent-blueprint-v1
---

Test-only Agent Blueprint pack for the per-agent A2UI catalog allowlist
(v15 S8). It declares exactly one builtin catalog, `clio-workspace`, and ships
no catalog of its own: the same declaration the shipped `base-agent` makes, kept
in-repo so the allowlist tests do not depend on the marketplace submodule.
