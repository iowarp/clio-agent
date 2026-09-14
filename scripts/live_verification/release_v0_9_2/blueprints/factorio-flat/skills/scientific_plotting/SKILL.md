---
name: scientific_plotting
title: Execute a Publication-Quality Scientific Figure
description: Apply the axis, unit, error-bar, and labeling conventions that make a figure self-contained and its precision honest, once the chart type is already chosen.
---

Use this skill once `data_visualization` has selected the chart type; this
skill covers executing it correctly. Label every axis with quantity and
units, and state the scale (linear, log, semi-log) explicitly — an
unlabeled or unit-less axis makes a figure unusable outside its immediate
context.

Show uncertainty on the figure itself: error bars (with what they represent —
standard deviation, standard error, a confidence interval — stated in the
caption) or a shaded confidence band, rather than relying on the text to
convey scatter the reader cannot see in the plot.

Write a caption that makes the figure self-contained: what was measured, the
conditions, sample size, and what the reader should conclude — a caption
that only repeats the axis labels adds nothing. Keep font sizes, line
weights, and symbol sizes legible at the figure's actual final printed or
displayed size, not just on a full screen.

Return the figure with labeled/unit-annotated axes, visible uncertainty
representation, and a self-contained caption.
