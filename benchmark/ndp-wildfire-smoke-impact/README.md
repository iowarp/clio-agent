# Case: NDP Wildfire Downwind Smoke & Air-Quality Impact

Status: **grounded, not yet built as a CLIO agent.** The question below was
solved manually against live NDP data first (see `manual-solution/`). The CLIO
Agent Blueprint and live-trace runs come next.

This spec is a contract, not a pass claim. A run passes only when a real CLIO
session (normal orchestrator, natural prompt) reproduces the grounded workflow
end-to-end with auditable per-branch evidence, a verified map artifact, and an
audited result note that matches the prompt intent. Run logs live in `runs/`,
never in this file.

## The question

> Active wildfires are burning across the western US. Which fire is actually
> putting smoke over people right now, where is that smoke going, and which
> communities are seeing the worst air quality? Show me a map.

It is natural (names no expert, tool, or schema) and its honest answer is a
layered map.

## Why it is hard (intrinsic, not bolted-on)

The difficulty is in the data path, not in artificial branch requirements:

- **The interesting fire is not the biggest fire.** Selecting by perimeter
  acreage is wrong — the large fires found live (Seven Cabins NM 71% contained,
  Santa Rosa Island CA 100% contained) had *no* smoke overhead. The case must
  select by **downwind impact**: a fire whose smoke-forecast footprint overlaps
  populated air-quality monitors. That selector is the core reasoning step and
  cannot be answered from a single catalog query.
- It requires fusing **three independent live feature services** in different
  schemas and coordinate systems, plus a geospatial join (smoke ∩ monitors,
  fire → region bbox).
- It must degrade honestly: out of fire season, or for a contained fire, the
  correct answer is "no significant downwind impact right now" — not a forced
  map. A contained-fire / no-smoke path is a required bounded outcome.

## Grounded solution path (verified live — see `manual-solution/DATASETS.md`)

1. **Discover active fires** — WFIGS current interagency fire perimeters
   (`attr_IncidentTypeCategory='WF'`). 69 active perimeters live at solve time.
2. **Select by downwind impact** — for candidate fires, query the NWS smoke
   forecast grid over a padded bbox; keep the fire(s) whose region carries smoke
   *and* contains AirNow monitors. (Manual solve used largest-Western and
   largest-CA fires and correctly found both contained / smoke-free — the
   negative result that proves the selector matters.)
3. **Resolve geography** — fire perimeter geometry → bbox (+~1.2° pad) as the
   analysis region.
4. **Overlay smoke** — NWS 48h smoke forecast polygons (`smoke_classdesc`
   µg/m³ class) intersecting the region.
5. **Overlay air quality** — AirNow current monitors in the region, colored by
   AQI category.
6. **Visualize + synthesize** — render perimeter + smoke + AQI on a basemap;
   brief which counties/communities are worst-affected, with caveats.

## Expected hierarchy (multi-branch)

```
main
├─ fire_discovery        (active perimeters, impact ranking)
├─ geography             (selected fire → region bbox)
├─ smoke_forecast        (smoke polygons over region)
├─ air_quality           (AirNow monitors over region)
├─ visualization         (layered map artifact)
└─ synthesis             (downwind impact brief + caveats)
```

Branches are real: smoke and air-quality acquisition are independent and only
join at fusion. No branch is mandatory by string contract — `synthesis` must be
reachable on the contained-fire / no-smoke path with a correct null result.

## Required evidence (per run, in `runs/`)

- Live provider + live NDP/feature-service calls (no mocks, no canned data).
- Per-branch outputs auditable at each boundary.
- A non-empty map artifact whose layers match the briefed claims.
- An audited note: selected fire + why, smoke footprint, worst AQI
  communities, and any honest "no impact" outcome.

## Visual

A single basemap map: fire perimeter (red), smoke forecast (greyscale by
concentration class), AirNow monitors (EPA AQI color scale). Far stronger demo
value than a single-series line plot. Manual-solve examples in
`manual-solution/*.png`.
