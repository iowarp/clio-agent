---
name: fatemi_socie
title: Apply the Fatemi-Socie Critical-Plane Fatigue Parameter
description: Compute and interpret the Fatemi-Socie damage parameter correctly for non-proportional multiaxial fatigue, with its material constant and search procedure stated.
---

Confirm the Fatemi-Socie method is the right choice: it is a shear-strain-
based critical-plane parameter suited to materials and loadings where shear
dominates crack initiation and where non-proportional hardening matters — see
`multiaxial_fatigue` to confirm the loading is non-proportional before
defaulting to this method over a simpler equivalent-stress approach.

State the damage parameter's form explicitly (shear strain amplitude on the
critical plane, scaled by a material constant k times the ratio of normal
stress to yield strength on that plane) and the material constant k used and
its source (fit to the material's own multiaxial data, not borrowed from an
unrelated material without justification).

Report the critical-plane search procedure: the parameter must be evaluated
over all candidate plane orientations at the point of interest and the
maximizing plane identified — a value computed on an assumed plane
(e.g., the plane of maximum principal stress) rather than the true maximizing
plane is not the Fatemi-Socie parameter.

Return the damage parameter value, the identified critical plane and its
orientation, the material constant used and its source, and the life
estimate the parameter maps to via the calibrated life curve.
