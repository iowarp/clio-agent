---
name: post_processing
title: Track Post-Processing Steps and the State They Produce
description: Treat each post-processing step as changing the part into a distinct, traceable state, rather than collapsing "as-built" and "finished" into one condition.
---

Enumerate the post-processing chain applied, in order: stress relief, hot
isostatic pressing (HIP — closes internal porosity under heat and pressure),
solution/age heat treatment, support removal, machining, and surface
finishing. Each step changes the part's state; a property or defect
measurement is only valid for the state it was measured at.

State what each step does and does not fix: HIP closes internal porosity but
does not remove surface-connected porosity or fix surface roughness; stress
relief reduces residual stress but does not change bulk microstructure the
way a full solution treatment does. Do not assume one step's benefit implies
another's.

Track state explicitly through the chain: as-built, stress-relieved, HIPed,
heat-treated, machined, finished — and label which state any reported
property, defect measurement, or characterization result describes.

Return the post-processing chain applied, what each step changed, the
resulting part state, and which state any associated measurement describes.
