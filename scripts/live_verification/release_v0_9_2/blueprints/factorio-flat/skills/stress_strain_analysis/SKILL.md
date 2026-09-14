---
name: stress_strain_analysis
title: Extract and Interpret Local Stress-Strain State From Simulation Output
description: Report a stress or strain result with the invariant, location, and material-model context that make it meaningful, rather than a single unqualified number.
---

State which stress or strain quantity is being reported — von Mises,
maximum principal, a specific tensor component, or an invariant appropriate
to the failure mode being assessed (maximum shear for ductile yielding,
maximum principal for brittle fracture, a critical-plane quantity for
multiaxial fatigue via `fatigue_failure_expert`). Reporting "the stress"
without naming the quantity is ambiguous and often the wrong one for the
question.

Report the location precisely: element/node, distance from a free surface or
stress concentration, and whether the value is a peak (single element) or an
averaged/extrapolated value — peak stress at a singular geometric feature
(sharp corner) is mesh-dependent and should be flagged as such rather than
reported as converged.

State the material model the stress-strain result assumes (linear elastic,
elastic-plastic with a specific hardening law) — a reported "stress" beyond
the material's yield point from a linear-elastic model is not a physically
meaningful stress value.

Return the stress/strain quantity, its location and mesh-convergence status,
and the material model it was computed under.
