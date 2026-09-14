---
name: boundary_conditions
title: Justify Boundary Conditions Against the Physical System They Represent
description: Treat every boundary condition and load as a modeling assumption that must match the real support and loading it stands in for, not a default the software supplies.
---

State what each boundary condition represents physically — a fixed support,
a symmetry plane, a bolted joint idealized as fully constrained — and whether
that idealization is conservative, unconservative, or unknown relative to the
real system. A "fixed" boundary condition is rarely physically exact; state
what it approximates and in which direction the approximation biases the
result.

Justify load application the same way: point loads representing a
distributed pressure, a simplified load case standing in for a real duty
cycle, or a load magnitude/direction taken from a separate analysis (e.g., a
predicted service load) — state the source and its own uncertainty.

Check for boundary-condition artifacts specifically: an overly stiff support
or a point load produces a local stress singularity that does not reflect
real behavior and should not be reported as a governing stress without
disclosing that it is a boundary-condition artifact, not a physical result.

Return each boundary condition and load with what it physically represents,
its known bias/approximation, and whether the reported result depends on a
boundary-condition artifact.
