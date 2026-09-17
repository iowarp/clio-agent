---
id: a2ui-second-pack
title: A2UI Second Pack
root_expert: root
a2ui_catalogs:
  second: catalogs/second
blueprint:
  format: agent-blueprint-v1
---

Test-only Agent Blueprint pack for
docs/design/a2ui-compat-campaign-2026-09.md S8's two-blueprint-isolation
tests (issue #1374 deliverable 3): a SECOND pack, distinct from
``a2ui_packs/minimal``, declaring its own catalog (`second`) so a test can
activate one blueprint per session and prove their producible catalog sets
differ.
