---
name: crack_initiation
title: Identify the Crack-Initiation Mechanism and Site
description: Determine whether initiation is surface- or defect-driven and at which specific site, since the mechanism determines which mitigation is actually effective.
---

Classify the initiation mechanism: surface-initiated (from surface roughness,
a machining mark, or a persistent slip band at a free surface — mitigated by
`surface_finishing` or shot peening) versus internal/defect-initiated (from a
subsurface pore or inclusion — mitigated by defect control in
`additive_manufacturing`/`pbf_lb` or by HIP in `post_processing`). Applying a
surface treatment does not address an internally-initiated failure, and vice
versa.

Use fractographic evidence (`fractography`) to locate the actual initiation
site on a failed specimen rather than assuming it from geometry alone — a
predicted stress concentration and the actual initiation site can disagree
when an internal defect happens to dominate over the nominal stress raiser.

State the initiation life fraction relative to total life when it can be
estimated (from fractography, from a striation count, or from a
crack-growth calculation in `fracture_mechanics`) — a short initiation life
relative to total life indicates a defect-dominated case, informing which
mitigation has the most leverage.

Return the identified initiation mechanism and site with its supporting
evidence, and which mitigation (surface treatment vs. defect control) the
finding actually supports.
