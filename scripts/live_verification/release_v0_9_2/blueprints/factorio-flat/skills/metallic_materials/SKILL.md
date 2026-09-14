---
name: metallic_materials
title: Reason Over Metallic Material Class Behavior
description: Apply the strengthening mechanisms, deformation behavior, and failure modes characteristic of a metal's crystal structure and alloy class before reasoning about a specific grade.
---

Identify the crystal structure and alloy class first (FCC/BCC/HCP; steel,
aluminum, nickel, titanium) — slip system count, ductility, and temperature
sensitivity follow directly from structure and constrain what strengthening
routes are even available.

Name the dominant strengthening mechanism for the class: solid-solution,
precipitation/age-hardening, work hardening, grain-refinement, or dispersion
strengthening, and state which one a given processing step is targeting.
Mechanisms combine, but their contributions are not simply additive without
evidence for the specific system.

State the class's characteristic failure and environmental sensitivities
(hydrogen embrittlement for high-strength steels, stress-corrosion cracking
for some aluminum tempers, creep for high-temperature nickel alloys) before
assuming a generic ductile-failure model applies.

Return the class-level reasoning (structure, strengthening mechanism,
characteristic failure modes) and hand off to the specific alloy/grade skill
(for example `nickel_superalloys`, `inconel_718`) for grade-specific claims.
