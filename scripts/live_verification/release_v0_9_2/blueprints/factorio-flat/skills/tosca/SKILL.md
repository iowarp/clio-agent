---
name: tosca
title: Formulate and Script a Tosca Topology Optimization Package
description: Turn an approved topology-optimization formulation into a Tosca Structure .par-driven package, distinct from Abaqus CAE's native optimization objects.
---

State the optimization task type Tosca is being asked to run (topology,
sizing, shape, bead) and, for topology, the objective and constraint
(compliance minimization under volume constraint is the common case, but not
the only one). A Tosca `.par` file drives the optimization loop around an
Abaqus solve — confirm the Abaqus input deck it references is itself valid and
converged before treating optimization non-convergence as a Tosca issue.

Declare design and frozen regions, symmetry, and every manufacturing
restriction the study needs (overhang angle for AM, extrusion/rotational
constraints, minimum member size, casting constraints) inside the `.par`
definition — restrictions declared informally in prose do not constrain the
optimizer.

Track the optimization's own convergence history (objective and constraint
evolution, iteration count) separately from whether the converged topology is
manufacturable (`manufacturability`) or structurally re-verified outside the
optimization loop (`finite_element_analysis`) — Tosca convergence answers only
the first.

Return the `.par` file content and referenced Abaqus deck, the declared
regions/restrictions, the optimization's convergence status, and the
traceability back to the approved formulation. Without executed solver
evidence, label it `GENERATED_NOT_EXECUTED`.
