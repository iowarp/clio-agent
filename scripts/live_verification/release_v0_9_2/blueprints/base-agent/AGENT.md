---
id: base-agent
title: Base Agent
display_name: Base Agent
version: 0.2.0
description: CLIO's configurable general-purpose agent with native workspace tools and no hidden routing hierarchy.
root_expert: base
blueprint:
  format: agent-blueprint-v1
experts:
  - experts/base.md
---

# Base Agent

One marketplace-owned agent with CLIO's native workspace tools. It handles
ordinary conversation and grounded workspace work without an internal fallback,
expert hierarchy, or hidden routing layer. It remains the clean TTFT/latency
baseline for any model while also serving as CLIO's default installed agent.

## The needle case (reproducible baseline)

1. Drop a `needle.md` in the workspace root containing a known number.
2. Attach this blueprint to a fresh session.
3. Ask: "Somewhere in this workspace there is a file named needle.md. Find
   it and tell me the number written inside it."
4. The run should take 2-4 LM calls (list/search, read, answer). Per-call
   TTFT from the stream audit log is the model's clean agent baseline,
   free of delegation resets and child spawn costs.
