---
id: a2ui-undeclared-pack
title: A2UI Undeclared Pack
root_expert: root
blueprint:
  format: agent-blueprint-v1
---

Test-only Agent Blueprint pack for the per-agent A2UI catalog allowlist
(v15 S8): it declares no `a2ui_catalogs` at all, so an agent bound to it has
no producible catalogs and gets no A2UI producer tools.
