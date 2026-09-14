---
name: resolve-earthscope-region
title: Resolve an EarthScope Region
description: Ground a place name or explicit coordinates into one fixed analysis region before any station or dataset claim.
---

Use this skill when a request names a geography that is not already represented
by a current, matching `workflow_state.geospatial` value.

If the user supplied latitude and longitude, preserve them verbatim and do not
geocode. Otherwise call `geo_geocode` once with the complete place name; for a US
place, include the state and country when available. If the first lookup is empty
or ambiguous, make one more lookup with a more specific query. Never replace a
failed lookup with model memory.

Use the user's radius exactly. If no radius was supplied, choose one conservative
regional radius, label it as an assumption, and keep it fixed for downstream
filtering. Record the resolved label, center latitude, center longitude, radius or
bounds, confidence, provenance, and warnings in `workflow_state.geospatial`.
Do not make any EarthScope availability claim in this step.

If a map would materially help the user, load `present-interactive-analysis`
and create or update the `earthscope-region` surface with the grounded center.
Do not render a map merely to decorate a coordinate answer.
