---
name: experimental_data_acquisition
title: Verify Data Acquisition Settings Against the Signal Being Measured
description: Check sampling rate, filtering, and channel calibration against the physical signal being captured, before treating logged data as a faithful record.
---

State the sampling rate relative to the fastest signal feature being
captured — a sampling rate too low relative to a fatigue test's cyclic
frequency or a dynamic event will alias or miss peak values; state the
margin used (e.g., a stated multiple of the highest frequency of interest).

Report any filtering applied (analog or digital, cutoff frequency, filter
order) — a filter that removes noise can also attenuate a real signal
feature if its cutoff is too close to the frequency of interest, and this
should be checked, not assumed safe.

Confirm each channel's calibration (load, strain, displacement, temperature)
is current and traceable, and record units and any scaling applied in the
acquisition software — a unit or scaling error at acquisition silently
propagates through every downstream analysis.

Return the sampling rate, filtering, and per-channel calibration status, and
confirm they are adequate for the signal the test is meant to capture.
