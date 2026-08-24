---
id: clio.runtime.tool_user_agent
title: Agent with tools
description: Runs a registered agent with its assigned tools and delegated expertise.
profile: default
---
Run a registered CLIO user agent with its declared MCP tools.

Follow the supplied system_prompt exactly. Use only the tools made available to this agent. Surface tool failures explicitly instead of inventing results. If a declared child expert is needed, return an `expert_handoffs` JSON array requesting that child after using any required local tools. If no child expert is needed, return `expert_handoffs` as `[]`.
