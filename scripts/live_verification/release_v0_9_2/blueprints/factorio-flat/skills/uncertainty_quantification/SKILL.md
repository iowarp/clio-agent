---
name: uncertainty_quantification
title: Quantify and Propagate Uncertainty Through a Result Chain
description: Track measurement, model, and sampling uncertainty separately and propagate them explicitly through a chain of calculations rather than reporting a single deterministic answer.
---

Classify each source of uncertainty in the chain: measurement uncertainty
(instrument resolution/calibration, per the specific technique's skill),
sampling uncertainty (from finite sample size — how many specimens, fields,
or scans), and model uncertainty (from an assumed material model, life model,
or simplification). Report which sources were actually quantified and which
were not.

Propagate uncertainty through a multi-step calculation explicitly — a
fatigue life prediction chaining a simulated stress, a measured defect size,
and a fitted life model should carry each input's uncertainty through to the
final result (via a stated method: analytical propagation, Monte Carlo
sampling), not report the final number as if it were exact.

State which input dominates the total uncertainty — this tells the scientist
where additional measurement or refinement would actually reduce uncertainty
in the final result, versus where further effort would not help.

Return the uncertainty sources identified, the propagation method used, the
resulting uncertainty on the final result, and the dominant contributing
source.
