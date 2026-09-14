---
name: porosity_analysis
title: Quantify Porosity and Distinguish Its Formation Mechanism
description: Classify porosity by formation mechanism and report its size/location distribution, not just a single volume-fraction number.
---

Classify porosity by mechanism before reporting a single "porosity" number:
gas/entrapped-gas porosity (typically spherical, from dissolved or entrapped
gas), keyhole porosity (irregular, near the melt-pool keyhole boundary, from
vapor-cavity collapse in AM), and lack-of-fusion porosity (irregular,
elongated, often with unmelted powder visible, from insufficient energy
input) — each implies a different process cause and a different fatigue
severity for the same volume fraction.

Report the size distribution and spatial location, not just total volume
fraction: fatigue life is typically governed by the largest pore near a
high-stress region (see `defect_driven_fatigue`), so a low average porosity
with one large near-surface pore can be more damaging than a higher but
uniformly distributed porosity.

State the measurement method and its resolution floor (`xct` for internal
porosity with its voxel-size limit, `microstructure_characterization` for a
2D cross-sectional estimate with its section-bias limit) — a reported
porosity number is only as trustworthy as the smallest pore the method could
detect.

Return the porosity classified by mechanism, its size/location distribution
(not just an average), and the measurement method and resolution floor
behind the numbers.
