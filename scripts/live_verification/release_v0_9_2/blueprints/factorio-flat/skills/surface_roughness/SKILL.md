---
name: surface_roughness
title: Report Surface Roughness With Its Measurement Parameter and Method
description: Attach a roughness value to the specific parameter, cutoff length, and measurement method that define it, since roughness numbers are not comparable without them.
---

State which roughness parameter is being reported — Ra (arithmetic mean),
Rz (mean peak-to-valley), Sa (areal mean, from an areal rather than profile
measurement) — and its evaluation/cutoff length. Ra and Sa are not the same
quantity and are not directly convertible without additional assumptions;
report the one actually measured.

State the measurement method (contact stylus profilometry, `optical_profilometry`,
areal optical scanning) and its own resolution and known limitations — a
contact stylus can under-resolve steep AM as-built features a stylus tip
cannot physically enter, while optical methods can be biased by highly
reflective or steeply sloped as-built surfaces.

For an AM surface specifically, state whether the measurement is on an
as-built, machined, or finished surface (see `surface_finishing`), and note
that as-built AM roughness is typically anisotropic (different on up-facing,
down-facing, and vertical surfaces per `build_orientation`) — a single
reported value should state which surface it came from.

Return the roughness parameter and value, its evaluation length and
measurement method, and the specific surface/orientation/finishing state it
describes.
