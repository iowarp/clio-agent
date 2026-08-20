---
id: orchestrator-worker
version: 0.1.0
title: Orchestrator + Worker (live-gate)
description: A tier-1 react orchestrator with one tier-2 worker child that holds native file/shell tools, for #1031 Pillar-2 loop-inbox live-gate validation (fire-and-forget completion injection).
root_expert: root
blueprint:
  format: agent-blueprint-v1
---

A minimal two-tier agent for the #1031 loop-inbox live gate. The `root`
orchestrator delegates to a single `worker` child that holds the native
shell/file tools, so a fire-and-forget child spawned mid-turn actually does real
work and completes — the substrate for validating mid-turn completion injection
into the parent's next ReAct step.
