---
name: audit-scientific-package
title: Audit a Scientific Simulation Package
description: Apply citation, physical, construction, traceability, and validation review without replacing scientific judgment with a score.
---

Read the actual dossier, cited sources, specification, and generated files.
Audit five linked surfaces:

1. Evidence: citations were read and support bounded claims; contrary evidence
   and search limits are visible.
2. Physics: units, magnitudes, signs, conservation, constitutive behavior, load
   path, constraints, and limiting behavior are plausible.
3. Traceability: major model and code choices map to accepted decisions or
   explicit proposals.
4. Construction: files are complete for the target interpreter and contain no
   hidden placeholders, undefined references, or invented inputs.
5. Validation: convergence, sensitivity, analytical or experimental benchmarks,
   and acceptance criteria can test the intended claim.

Report evidence-linked findings with severity. Never upgrade generated code to
executed or validated without real solver artifacts.
