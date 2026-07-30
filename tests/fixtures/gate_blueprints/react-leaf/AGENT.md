---
id: react-leaf
version: 0.1.0
title: React Leaf (live-gate)
description: A plain single-expert react agent with native file + shell tools, for #1031 live-gate validation of permissions and provenance.
root_expert: root
blueprint:
  format: agent-blueprint-v1
---

A minimal leaf react agent used by `scripts/live_gate_1031.py` (epic #1031 live
gate). One tier-1 react expert that holds the native workspace tools directly
(shell + file read/write) plus the always-attached `create_artifact`, so a live
turn actually produces and transforms files on disk — the substrate the
permission and provenance pillars are validated against.
