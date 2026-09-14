---
name: finite_element_analysis
title: Formulate a Finite Element Study From Question to Conclusion
description: Trace one continuous argument from the physical question through idealization, discretization, and solution to what the outputs can actually support.
---

Trace one continuous chain: research question and quantity of interest,
physical idealization (2D/3D, linear/nonlinear, static/dynamic), geometry,
material model, `boundary_conditions`, `meshing`, solver procedure, and
outputs. A choice at any link (symmetry assumption, contact definition,
element type) is a modeling decision that changes what the result means, not
a numerical detail.

State the idealizations made and their justification: small vs. large
deformation, linear-elastic vs. plastic material response, whether the
loading is quasi-static or requires a dynamic/fatigue-cycle treatment. An
idealization valid for one quantity of interest (peak stress) may not be
valid for another (fatigue life, requiring the full cycle).

Never claim converged or validated results without the checks that support
that claim (mesh convergence, sensitivity to boundary-condition assumptions,
comparison to an analytical or experimental benchmark). Without executed
solver evidence, label the study `GENERATED_NOT_EXECUTED`.

Return the full chain from question to output, the idealizations and their
justification, and the convergence/validation evidence — or its absence —
behind the result.
