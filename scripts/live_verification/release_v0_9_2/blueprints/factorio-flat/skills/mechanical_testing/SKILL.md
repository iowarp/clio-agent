---
name: mechanical_testing
title: Design a Mechanical Test Against a Named Standard
description: Specify a mechanical test's type, control mode, specimen geometry, and governing standard before running or interpreting it.
---

State the test type and its governing standard explicitly (ASTM E8 for
tension, ASTM E466/E606 for fatigue, etc.) — a test described only as
"we pulled it to failure" or "we cycled it" is not reproducible or
comparable to another lab's result.

Specify the control mode and its consequence: load-controlled testing holds
nominal stress constant and lets strain respond (appropriate for high-cycle,
elastic-dominated fatigue), while strain-controlled testing holds strain
constant (needed for low-cycle fatigue where plastic strain dominates). Using
the wrong control mode for the regime under study invalidates the comparison
to standard S-N or strain-life data.

State specimen geometry and its effect: gauge section size/shape,
surface finish (machined, as-built, polished), and orientation (for
anisotropic material, relative to the property axis) all change the result
independent of the material itself — a specimen effect can be mistaken for a
material effect if not controlled or reported.

Return the test type, standard, control mode, and specimen specification the
result depends on, before interpreting the measured behavior.
