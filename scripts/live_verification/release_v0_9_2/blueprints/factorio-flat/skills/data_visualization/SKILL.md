---
name: data_visualization
title: Choose a Visualization That Matches the Comparison Being Made
description: Select the chart type and encoding that actually represents the comparison or relationship in the data, rather than a default plot type.
---

Start from the comparison being made, not the data type: a distribution
comparison (fatigue life scatter between two conditions) needs a different
representation (box plot, probability plot) than a trend (S-N curve, a
property vs. a process parameter) or a relationship between two continuous
variables (scatter plot with a fit). Choosing a chart type before naming the
comparison usually produces a chart that technically shows the data but does
not make the actual claim visible.

For fatigue and reliability data specifically, prefer a representation suited
to censored/scattered data (an S-N plot with runouts marked distinctly, a
Weibull probability plot) over a plot type that implicitly assumes complete,
un-censored data.

State what the visualization is claiming and check it against the
underlying statistics (`statistics`) — a log-scale axis, a truncated axis, or
an omitted data point can visually imply a stronger or weaker effect than the
data supports.

Return the chosen chart type and encoding with the comparison it represents,
and hand off to `scientific_plotting` for the specific figure execution and
formatting.
