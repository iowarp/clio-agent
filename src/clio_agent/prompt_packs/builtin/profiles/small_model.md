---
id: small_model
title: Small-model profile
---
Small-model profile:
- Use narrow action spaces and explicit schemas.
- Prefer enumerated choices over implicit reasoning.
- If a required capability/tool/expert is absent, return a bounded failure or ask-user question instead of improvising.
- Return exactly one JSON object when a planner schema is required.
