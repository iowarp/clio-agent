---
name: am_process_parameters
title: Record and Reason Over the Full AM Process Parameter Set
description: Treat the complete process parameter set — not just the headline energy-density number — as the object that determines part quality and reproducibility.
---

Record the full parameter set relevant to the process family: for powder bed
fusion, laser power, scan speed, hatch spacing, layer thickness, scan
strategy, and preheat/chamber temperature; for DED, deposition rate, travel
speed, and interpass temperature. A single derived metric (energy density)
discards information the individual parameters carry separately.

State which parameters were validated for the specific material/machine
combination versus assumed from a similar system — a parameter set tuned on
one powder lot, machine, or alloy does not transfer without requalification,
because absorptivity, thermal conductivity, and melting range differ by
material.

Distinguish a nominal (programmed) parameter from a verified (measured)
one — actual laser power, layer thickness, and oxygen level can drift from
the programmed value, and a defect investigation should check which was
used.

Return the full parameter set with its material/machine qualification status,
and flag any parameter that is nominal rather than verified.
