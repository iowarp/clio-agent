---
name: xct
title: Characterize a Part With X-Ray Computed Tomography
description: Connect XCT acquisition and segmentation choices to what the resulting pore/defect statistics can actually support, before treating them as ground truth.
---

Record the acquisition conditions: voxel size, source/detector geometry
(cone-beam vs. others), and known artifacts — beam hardening, ring
artifacts, scatter, and metal-streaking for dense/high-atomic-number
materials. Voxel size sets a hard floor on the smallest reliably detected
feature; a pore near or below that size is not reliably sized or counted.

Treat segmentation as a modeling choice, not a measurement: state the
threshold or segmentation method used and its validation against a known
reference (a calibration phantom, an independent technique, or a held-out
labeled region), and report the sensitivity of the resulting statistics (pore
volume fraction, size distribution) to plausible threshold changes —
partial-volume blur at boundaries routinely biases size and count.

For a defect-driven fatigue study specifically, report not just total
porosity but the size and spatial distribution of the largest pores, since
fatigue life is typically governed by the worst individual defect near a
high-stress region, not by average porosity.

Return the acquisition conditions and artifacts, the segmentation method and
its sensitivity, and the pore/defect statistics (with the largest-defect
distribution stated separately from the average) needed for
`defect_characterization` or `defect_driven_fatigue`.
