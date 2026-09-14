---
name: morphorm
title: Formulate a Morphorm-Based Topology Optimization Study
description: Apply Morphorm's optimization workflow and solver interface as an alternative topology-optimization route to Tosca, without conflating the two toolchains' conventions.
---

State which solver backend Morphorm is driving for this study and confirm
its input format and unit conventions explicitly — Morphorm's own
configuration schema, mesh format, and constraint declarations are not
interchangeable with a Tosca `.par` file even when the optimization problem
(objective, constraints, design/frozen regions) is conceptually the same.

Declare the objective, constraints, design/frozen regions, and any
manufacturing restrictions (overhang, extrusion, minimum feature size) in
Morphorm's own configuration format, and confirm the referenced analysis
model (mesh, materials, loads) is valid on its own before attributing an
optimization failure to the optimizer.

Track the optimization's convergence history separately from the
manufacturability and structural re-verification of the converged result,
the same separation `topology_optimization` requires regardless of which
solver produced the topology.

Return the Morphorm configuration and referenced analysis model, the declared
regions/restrictions, the optimization's convergence status, and traceability
back to the approved formulation. Without executed solver evidence, label it
`GENERATED_NOT_EXECUTED`.
