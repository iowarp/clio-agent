---
id: data-exploration
version: 0.1.0
title: Data Exploration/Search Agent
description: Scientific data discovery, inspection, analysis, visualization, and local utility work.
root_expert: main
blueprint:
  format: agent-blueprint-v1
defaults:
  prompt_profile: default
requires:
  memory_tools:
    - memory_search_sessions
    - memory_read_session_summary
    - memory_read_context_frame
---

The default CLIO Agent for scientific data exploration and search.
