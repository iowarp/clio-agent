---
name: microstructure_characterization
title: Measure Microstructure From Optical, Electron, or EBSD Images
description: Turn optical, electron, or EBSD images into quantitative microstructure measurements while controlling for sampling bias, calibration, and preparation artifacts.
---

Record the technique and conditions: modality (OM, SEM, TEM, EBSD), signal,
magnification and pixel calibration, accelerating voltage, detector, and the
specimen preparation route. Many features reported from micrographs are
preparation artifacts: pull-out, relief, redeposition, ion-beam damage,
etching bias, and charging. Rule these out before measuring.

For quantitative work, define the measurand and the standard: grain size by a
named method (for example ASTM E112 intercept or planimetric), phase fraction
by systematic point or area counting, particle size and spacing by a stated
stereological rule. State the number of fields, total area or line length,
and the resulting sampling uncertainty. A single representative image is not
a measurement.

Guard against section bias: a 2D section under-samples large features and
mis-measures 3D size and shape unless corrected. For EBSD, report step size,
indexing rate, and cleanup, and do not treat cleaned-up data as raw. For 3D
internal features, use `xct` rather than inferring 3D structure from a 2D
section.

Return the measurements with uncertainties and method, the artifacts
considered and excluded, the sampling adequacy, and the microstructural
claims the images cannot support — feeding results into `microstructure`.
