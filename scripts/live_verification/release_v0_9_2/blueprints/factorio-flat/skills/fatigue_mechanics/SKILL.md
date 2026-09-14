---
name: fatigue_mechanics
title: Reason Over Fatigue Damage Mechanisms and Regime Selection
description: Identify the fatigue regime and controlling mechanism before selecting a life-prediction method, since the wrong regime assumption invalidates the method choice.
---

Identify the fatigue regime by mechanism, not just cycle count: high-cycle
fatigue (elastic-dominated, typically above ~10^4-10^5 cycles, use
`high_cycle_fatigue`) versus low-cycle fatigue (plastic-strain-dominated,
fewer cycles, needs strain-based rather than stress-based life models). The
boundary is material- and loading-dependent, not a fixed cycle count.

Name the damage mechanism stage: crack initiation (nucleation at a
persistent slip band, surface defect, or internal defect — see
`crack_initiation`) versus crack growth to critical size (see
`fracture_mechanics`) versus final fracture. Total life is the sum of
initiation and growth life, and their relative share changes which mechanism
dominates the result and which mitigation (surface treatment vs. defect
control) is effective.

State whether the loading is uniaxial or multiaxial (`multiaxial_fatigue`),
and whether a defect is expected to control initiation
(`defect_driven_fatigue`) — each changes the applicable life-prediction
method in `fatigue_life_prediction`.

Return the identified regime, mechanism stage, and loading type, and the
specific life-prediction method they imply.
