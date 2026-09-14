---
name: optical_profilometry
title: Acquire and Interpret Optical Profilometry Data
description: Configure an optical profilometer for the surface being measured and know where the technique's own artifacts can bias a roughness or topography result.
---

State the technique variant (confocal, focus-variation, white-light
interferometry) and its lateral and vertical resolution — these differ by
orders of magnitude between variants and set what surface features can
actually be resolved.

Check for the technique's characteristic failure modes on the specific
surface: data dropout on highly reflective or very dark regions, and bias on
steeply sloped surfaces (a common issue on as-built AM parts) where the
technique cannot capture a valid reflection — report the fraction of the
scan area that had to be interpolated or excluded due to dropout, since a
high dropout fraction undermines confidence in the reported roughness.

State the filtering applied to separate roughness from waviness and form
(a Gaussian or robust filter with its cutoff), since the same raw scan can
produce different reported roughness values under different filter settings
— see `surface_roughness` for which parameter and cutoff to report.

Return the technique, resolution, dropout/artifact assessment, and filtering
applied, feeding the resulting parameter into `surface_roughness`.
