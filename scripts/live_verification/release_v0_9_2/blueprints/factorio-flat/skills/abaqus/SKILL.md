---
name: abaqus
title: Script and Audit an Abaqus Package
description: Turn an approved simulation formulation into an auditable Abaqus Python/input-deck package without claiming execution.
---

Target the declared Abaqus release, embedded Python interpreter (Abaqus
scripting uses Python 2 on older releases, Python 3 on newer ones — state
which), unit system, license, and modules (Standard/Explicit,
CAE/CATIA/Tosca add-ons). Build the complete required chain: part/geometry
or import, material and section assignment, assembly, step/procedure,
interactions and constraints, loads and `boundary_conditions`, `meshing`, and
requested outputs/history requests.

Use stable named sets and surfaces rather than positional references, so the
model remains editable and auditable. Keep Abaqus CAE's native optimization
objects distinct from a Tosca `.par`-driven workflow — they are different
mechanisms even when both are called "topology optimization" inside Abaqus.

Every generated package declares units, target release/interpreter,
license/module assumptions, external inputs, run commands, expected outputs,
and traceability back to the approved formulation. Without returned solver
evidence its status is `GENERATED_NOT_EXECUTED`; a ready package has no TODOs,
ellipses, undefined names, or invented files.

Return the exact file content, manifest, environment assumptions, run
commands, expected outputs, and verification checks for the generated
package.
