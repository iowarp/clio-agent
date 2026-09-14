---
name: fatigue_testing
title: Run and Report a Fatigue Test Campaign
description: Specify the loading, control mode, and runout convention of a fatigue test campaign so its S-N or strain-life data is comparable and correctly censored.
---

State the loading explicitly: stress or strain amplitude/range, mean stress
or R-ratio, waveform, and frequency, per the governing standard (ASTM
E466 for load-controlled axial fatigue, E606 for strain-controlled). A single
"cycles to failure" number without R-ratio and amplitude is not usable
S-N/strain-life data.

Define the failure and runout criteria before testing: what counts as
failure (complete separation, a stated stiffness drop, a crack of stated
length) and the runout cycle count beyond which a specimen is stopped and
censored rather than run to actual fracture. Runout data points are
censored, not failures, and must be treated as such in any statistical
fit (see `data_analysis_expert`'s `statistics`), not dropped or counted as
failures at the runout cycle count.

Report specimen-to-specimen scatter honestly — fatigue life scatter of an
order of magnitude at fixed stress amplitude is common and expected, not a
sign of a bad test, and a single specimen result does not establish an S-N
point without repeats or a stated confidence level.

Return the loading and control mode, failure/runout criteria, the raw
life data with censoring flagged, and the specimen/geometry/orientation the
data applies to.
