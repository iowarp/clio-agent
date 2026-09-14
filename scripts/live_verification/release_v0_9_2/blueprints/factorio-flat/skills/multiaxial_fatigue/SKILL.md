---
name: multiaxial_fatigue
title: Recognize and Analyze Multiaxial Fatigue Loading
description: Detect when a stress state is genuinely multiaxial and non-proportional, and route to a critical-plane or equivalent-stress method capable of handling it.
---

Check the stress state at the location of interest, not just the nominal
applied load: a nominally uniaxial applied load can produce a multiaxial
local stress state at a notch, fillet, or contact patch due to constraint —
use the local stress/strain state from `stress_strain_analysis`, not the
remote applied load, to decide whether multiaxial analysis is needed.

Distinguish proportional multiaxial loading (principal stress directions
fixed in time, ratio of components constant) from non-proportional loading
(directions or ratios rotate through the cycle) — a simple equivalent-stress
approach (von Mises amplitude) can be adequate for proportional loading but
is known to be non-conservative for non-proportional loading, where a
critical-plane method (`fatemi_socie`) is generally needed.

State which stress/strain components were used to determine proportionality
and over what portion of the loading cycle — a conclusion of "proportional"
based on only the peak load misses rotation that occurs elsewhere in the
cycle.

Return the local stress state and its proportionality classification, and
the fatigue method (`high_cycle_fatigue` equivalent-stress vs. a
critical-plane method) it implies.
