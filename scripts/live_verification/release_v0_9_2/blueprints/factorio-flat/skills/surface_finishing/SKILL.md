---
name: surface_finishing
title: Reason Over Surface-Finishing Process Choice and Its Effect
description: Match a surface-finishing process to the surface state it needs to change, and state what it changes versus what it leaves unaffected.
---

State the starting surface condition (as-built AM roughness, as-machined) and
the target requirement (roughness value, fatigue performance, aesthetic) before
selecting a finishing process — the required process differs by orders of
magnitude in material removal between light polishing and abrasive flow
machining or shot peening.

Name the process and its mechanism: machining/grinding removes material to a
controlled geometry, abrasive/chemical/electrochemical polishing reduces peak
roughness without necessarily removing subsurface defects, and shot peening
or laser peening introduces compressive residual stress at the surface
primarily for fatigue benefit rather than for roughness reduction. Do not
conflate a roughness-reduction process with a residual-stress process; a
finishing choice may need to do both, deliberately.

For internal/hard-to-reach AM surfaces (internal channels, lattice
structures), state whether the chosen process can physically access the
surface in question — a process validated on external surfaces does not
automatically reach internal ones.

Return the process selected, its mechanism and what it changes (roughness,
residual stress, geometry), and whether it was verified to reach every
surface the requirement applies to.
