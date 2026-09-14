---
name: topology_optimization
title: Formulate and Interpret a Topology Optimization Study
description: State the optimization problem precisely — objective, constraints, design/frozen regions, and manufacturing restrictions — and treat its result as a design proposal, not a finished part.
---

State the optimization problem explicitly before running or interpreting a
study: objective (minimize compliance, maximize stiffness-to-weight, minimize
mass under a stress constraint), constraints (volume fraction, stress,
displacement), design region versus frozen/non-design region, and any
symmetry or manufacturing restriction (extrusion, overhang-angle, minimum
member size) applied during the optimization itself.

Distinguish the optimization's own convergence (did the algorithm converge to
a stable topology under its stated objective/constraints) from the
optimized geometry's manufacturability and structural performance under the
real loading — an optimizer satisfying its own objective function does not
guarantee the result is buildable (`manufacturability`) or performs as
intended once meshed and re-verified with `finite_element_analysis` outside
the optimization loop.

State which manufacturing restrictions, if any, were applied inside the
optimization (Abaqus/Tosca and Morphorm both support overhang and extrusion
constraints) versus checked afterward — a restriction applied after the fact
changes a converged topology and requires re-verification.

Return the optimization problem statement, the converged result and its
constraint satisfaction, and the manufacturability and re-verification status
of the resulting geometry.
