---
id: tool-ui-cross-file-triage
title: Tool UI Cross-file Triage
display_name: Tool UI Cross-file Triage
version: 0.1.0
description: A contained qualification fixture for parallel file investigation, status, wait, collection, and causal joins.
root_expert: main
blueprint:
  format: agent-blueprint-v1
experts:
  - experts/main.md
  - experts/gateway.md
  - experts/notifications.md
defaults:
  prompt_profile: heavy
---

# Tool UI Cross-file Triage

This workspace-scoped blueprint exists only for the cross-file tool UI
qualification scenario. It reads supplied local fixtures and never uses external
services or modifies workspace files.

