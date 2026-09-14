---
name: defect_driven_fatigue
title: Predict Fatigue Life From a Measured, Specific Defect
description: Connect a measured defect's size and location to the local stress state at that exact location, and use a defect-size-based life model rather than a smooth-specimen S-N curve.
---

Use this skill when a specific defect (pore, inclusion, lack-of-fusion
region) is known to control or is suspected to control fatigue initiation —
common for additively manufactured parts (see `porosity_analysis`,
`xct`) — rather than assuming an idealized defect-free material.

Connect the defect's measured size (typically an equivalent area or
√area metric) and its exact location to the local stress state at that
specific location from `stress_strain_analysis`, not a nominal or
section-average stress — fatigue life from a defect-driven model is
governed by the combination of defect size and the local stress it actually
sits in, and a surface-breaking defect is generally more damaging than an
internal one of the same size at the same nominal stress.

State the life model used (a √area-based approach such as Murakami's, or a
fracture-mechanics treatment of the defect as an initial crack via
`fracture_mechanics`) and its calibration material/condition — a model
calibrated on one alloy's defect population does not transfer to another
without validation.

Return the defect size/location used, the local stress state at that
location, the life model and its calibration, and the predicted life with
its sensitivity to the assumed defect size.
