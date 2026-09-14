---
name: additive_manufacturing
title: Reason Over Additive Manufacturing Process Families and Their Consequences
description: Name the AM process family precisely and trace it to the defect and anisotropy population it characteristically produces, before any process-specific reasoning.
---

Name the process family precisely — powder bed fusion (laser or electron
beam), directed energy deposition, binder jetting, material extrusion, sheet
lamination — before reasoning about outcomes. Defect mechanisms, achievable
tolerances, and applicable materials are process-family-specific and do not
transfer across families.

State what is generic to all AM processes versus what is family-specific:
layer-wise build-up (generic) versus the specific energy source, feedstock
form, and consolidation mechanism (family-specific). Anisotropy from the
build direction and a dependency of final properties on process parameters
are near-universal across AM; the specific mechanism producing them is not.

For a powder-bed-fusion study, use `pbf_lb` for the process-specific
parameters and defect mechanisms rather than reasoning generically here.

Return the process family identified, what about the outcome is generic to
AM versus family-specific, and hand off to the family-specific skill for
parameter-level reasoning.
