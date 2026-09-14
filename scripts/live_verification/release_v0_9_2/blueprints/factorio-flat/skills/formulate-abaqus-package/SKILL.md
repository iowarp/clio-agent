---
name: formulate-abaqus-package
title: Formulate an Abaqus Package
description: Turn an approved scientific question into an auditable simulation specification and generated Abaqus/Tosca handoff.
---

Trace one continuous argument from research question and quantity of interest
through physical idealization, geometry, materials, loads, constraints,
interactions, discretization, solver procedure, outputs, sensitivity and
validation to the conclusion the outputs may support.

Do not treat boundary conditions, material law, optimization responses, or
validation as code details. Obtain explicit scientist decisions for choices that
change the physical claim. Label safe numerical defaults and plan sensitivity
checks.

Every generated package declares units, target Abaqus release and interpreter,
license/module assumptions, external inputs, commands, expected outputs,
traceability, and verification checks. Without returned solver evidence its
status is `GENERATED_NOT_EXECUTED`.
