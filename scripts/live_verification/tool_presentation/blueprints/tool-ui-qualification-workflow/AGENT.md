---
id: tool-ui-qualification-workflow
title: Tool UI Qualification Workflow
display_name: Tool UI Qualification Workflow
version: 0.1.0
description: A contained qualification fixture for ordinary skills, child-task skills, and declared workflow presentation.
root_expert: main
blueprint:
  format: agent-blueprint-v1
experts:
  - experts/main.md
  - experts/inventory.md
  - experts/verification.md
defaults:
  prompt_profile: heavy
---

# Tool UI Qualification Workflow

This workspace-scoped blueprint exists only to exercise the declared skill and
workflow contracts with deterministic, supplied facts. It does not access
external services or modify workspace files.

