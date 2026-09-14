---
name: manufacturability
title: Check a Design Against Its Manufacturing Process Constraints
description: Evaluate whether a proposed geometry — often a topology-optimized one — can actually be produced by the intended process, before treating it as a manufacturable part.
---

Check the design against process-specific geometric constraints before
treating it as buildable: minimum feature size and wall thickness, maximum
unsupported overhang angle, enclosed/trapped-powder volumes (critical for
powder bed fusion — trapped powder cannot be removed and adds mass and
potential contamination), and minimum hole/channel diameter for the intended
process.

A topology-optimized geometry is a design proposal, not a manufacturability
guarantee: complex organic geometry commonly produced by topology optimization
frequently violates overhang and trapped-volume constraints, and reconciling
the two is a distinct step (manufacturing-constrained optimization or manual
redesign), not automatic.

State which constraints were checked, which were violated, and whether a
violation was resolved by redesign, added supports, or an accepted process
risk — do not report a geometry as manufacturable without stating which
checks were actually performed.

Return the manufacturability assessment: constraints checked, violations
found, and how each was or was not resolved before the design is released to
the manufacturing process.
