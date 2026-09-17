---
id: a2ui-minimal-pack
title: A2UI Minimal Pack
root_expert: root
a2ui_catalogs:
  minimal: catalogs/minimal
blueprint:
  format: agent-blueprint-v1
---

Test-only Agent Blueprint pack for
docs/design/a2ui-compat-campaign-2026-09.md S2's catalog registry tests: it
declares one pack catalog (`minimal`) that aliases the official Basic
`Text`/`Button` components under new names (`MinimalText`/`MinimalButton`),
proving a pack can compose the renderer's existing kernels under its own
vocabulary without any pack-shipped renderer code.
