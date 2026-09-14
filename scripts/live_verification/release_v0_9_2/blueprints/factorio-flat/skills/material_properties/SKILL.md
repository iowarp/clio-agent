---
name: material_properties
title: State and Bound a Material Property Claim
description: Attach a material property to its measurement method, condition, and source class before it is used in a design or simulation decision.
---

Classify the property's source before using it: measured (with method and
specimen state), datasheet minimum/typical (a statistical bound, not a
measured value for the specific lot), or model-predicted (with the model's own
assumptions). These are not interchangeable, and a design decision should
state which one it relies on.

State the condition the property was obtained under: temperature, strain
rate, environment, orientation (for anisotropic material), and specimen
geometry/size. A property reported without its condition is incomplete —
yield strength, fatigue limit, and fracture toughness are all
condition-sensitive.

Distinguish a property that is intrinsic to the material composition/phase
state from one that is extrinsic (geometry- or process-dependent, like
fracture toughness in thin sections, or fatigue strength in the presence of
surface roughness or porosity) — extrinsic properties do not transfer between
parts with different geometry or processing history.

Return the property value with its source class, measurement/model
conditions, and the specific part or design condition it is (or is not)
valid for.
