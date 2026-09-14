---
name: pbf_lb
title: Reason Over Laser Powder Bed Fusion Process-Defect Linkages
description: Connect laser powder bed fusion process parameters and scan strategy to the specific defect population, melt pool behavior, and microstructure they produce.
---

State the parameter set that controls the melt pool regime: laser power,
scan speed, hatch spacing, layer thickness, and the derived volumetric energy
density — and name the melt pool regime it produces (conduction, keyhole,
lack-of-fusion-prone) rather than reporting energy density alone, since the
same energy density can arise from different power/speed combinations with
different melt pool behavior.

Connect regime to defect: keyhole-mode melting produces keyhole porosity from
vapor-cavity collapse; insufficient energy density or poor overlap produces
lack-of-fusion porosity (irregular, often with unmelted powder visible);
spatter and denudation affect surface quality and can seed near-surface
defects. State which regime and which defect mechanism a given parameter set
implies, using the process's characteristic melt-pool physics, not just a
correlation.

Account for scan strategy (island/stripe patterns, rotation between layers)
and its effect on residual stress and columnar grain growth direction
relative to the build axis — see `build_orientation` for the resulting
anisotropy. Powder reuse and oxygen content are process-parameter-adjacent
factors that also shift the defect population.

Return the parameter set and melt-pool regime, the defect mechanism it
implies, and the microstructural/anisotropy consequence via `build_orientation`.
