---
name: structural_analysis
title: Reason Over Structural Response Under Load
description: Connect a loading scenario to the structural response mode it actually produces — stiffness, stability, or strength-limited — before selecting an analysis type.
---

Identify which structural response the question is actually about: stiffness
(deflection under load), stability (buckling), or strength (yield/ultimate
capacity, fatigue) — the analysis type, mesh density, and material model
needed differ for each, and a study set up for one does not automatically
answer another.

State the load path explicitly: how load enters the structure, how it is
carried to the supports, and where it concentrates (fillets, holes,
section changes, joints). A structural analysis that reports a global result
(maximum stress) without identifying the load path cannot explain why that
location governs.

Distinguish a linear-elastic structural analysis (valid for stress below
yield, small deflection) from cases requiring `finite_element_analysis`'s
nonlinear treatment (large deflection, contact, material plasticity,
buckling past the linear regime) — using a linear result outside its
validity range overstates margin or understates deflection.

Return the identified response mode, the load path and critical locations,
and the analysis type and its validity range behind the reported result.
