---
name: fracture_mechanics
title: Apply Linear-Elastic Fracture Mechanics to a Known or Assumed Defect
description: Apply a stress-intensity or crack-growth approach with the defect geometry, loading, and material toughness stated explicitly, rather than assuming LEFM validity.
---

Confirm linear-elastic fracture mechanics applies before using it: it
requires small-scale yielding at the crack tip relative to specimen/part
dimensions — for a ductile material with a large plastic zone relative to
the section, an elastic-plastic fracture mechanics approach is needed
instead, and applying LEFM anyway is non-conservative or meaningless.

State the crack/defect geometry and its stress-intensity solution source
(a handbook solution, a calibrated finite-element result) — the geometry
factor Y in K = Y·σ·√(πa) depends on defect shape, location (surface,
corner, embedded), and component geometry, and using the wrong geometry
factor is a common source of error.

For fatigue crack growth specifically, state the growth law used (Paris law
or another) and its calibrated constants' source and applicability range —
near-threshold and near-critical growth rates deviate from the mid-range
Paris regime, and applying mid-range constants outside that regime
misestimates growth rate.

Return the defect geometry and stress-intensity solution, the material
toughness/growth-law constants and their source, and the resulting
critical crack size or remaining-life estimate with its validity range.
