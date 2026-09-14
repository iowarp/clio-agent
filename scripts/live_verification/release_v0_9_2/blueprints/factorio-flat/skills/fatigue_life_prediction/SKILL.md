---
name: fatigue_life_prediction
title: Select and Apply a Fatigue Life-Prediction Method With Its Inputs Traced
description: Choose a life-prediction method that matches the regime and loading actually present, and trace every input it consumes back to its source.
---

Select the method from the regime and loading already established
(`fatigue_mechanics`, `multiaxial_fatigue`): S-N/`high_cycle_fatigue` for
elastic-dominated uniaxial loading, a critical-plane method
(`fatemi_socie`) for multiaxial non-proportional loading, a
fracture-mechanics crack-growth approach (`fracture_mechanics`) when an
initial defect size is known and the question is remaining life, or a
defect-driven approach (`defect_driven_fatigue`) when a specific measured
defect controls initiation.

Trace every input the method consumes to its source: local stress/strain
state from simulation (`stress_strain_analysis`), material cyclic
properties from testing or literature (state which), and any defect size
from characterization (`porosity_analysis`, `xct`) — a life prediction is
only as reliable as its least-certain input, so state which input dominates
the uncertainty.

Report the predicted life with its uncertainty band, not a single number —
scatter in fatigue data (see `fatigue_testing`) and input uncertainty both
propagate into the prediction, and a design decision needs the band, not
just the mean estimate.

Return the method selected and why, every input traced to its source, and
the predicted life with its uncertainty and the input that dominates it.
