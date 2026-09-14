---
name: high_cycle_fatigue
title: Apply Stress-Based (S-N) High-Cycle Fatigue Analysis
description: Apply an S-N life estimate with its mean-stress correction and fatigue-limit assumption stated explicitly, rather than reading a single curve without qualification.
---

Confirm the regime is actually elastic-dominated (high-cycle, per
`fatigue_mechanics`) before applying a stress-based method — a strain-based
approach is needed once plastic strain is significant.

State the S-N curve's source and applicability: material, surface condition,
specimen geometry, and R-ratio/mean stress it was generated at. Using a
smooth-specimen S-N curve for a part with surface roughness, a notch, or
internal defects overstates life unless a correction (surface factor, notch
factor, or a defect-driven approach) is applied — do not apply a handbook
curve to a part it was not representative of.

State the mean-stress correction used (Goodman, Gerber, Walker, or none) when
the service R-ratio differs from the test R-ratio the curve was generated
at, since mean stress materially changes life for a given stress amplitude.

Return the S-N curve and its source/applicability, the mean-stress
correction applied, and the resulting life estimate with its stated
confidence or scatter band.
