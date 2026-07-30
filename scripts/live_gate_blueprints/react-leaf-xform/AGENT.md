---
id: react-leaf-xform
version: 0.1.0
title: React Leaf + Transform (live-gate)
description: A single-expert react agent with native file/shell tools PLUS the in-process summarize_csv transform tool, for #1031 Pillar-3 cross-job provenance validation.
root_expert: root
mcp_servers:
  xform: D:/Libraries/Documents/projects/clio-agent/.venv/Scripts/python.exe D:/Libraries/Documents/projects/clio-agent-1031/scripts/live_gate_blueprints/transform_stdio.py
blueprint:
  format: agent-blueprint-v1
---

Leaf react agent for the #1031 provenance gate. Identical to `react-leaf` but its
root expert also declares `xform_summarize_csv` — a designated-output transform
tool mounted in-process by `scripts/live_gate_boot.py`. A single `summarize_csv`
call reads an input CSV (a `used` edge, resolved cross-workspace) and writes a
designated output (a `generated` edge), so `b = transform(a)` lineage chains across
jobs.
