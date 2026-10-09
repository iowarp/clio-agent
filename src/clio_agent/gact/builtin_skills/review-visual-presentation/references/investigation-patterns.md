# Investigation and presentation patterns

## Map: which storm passed near Chicago?

Inspect the archive's time, position and storm-identity columns and the
question's meaning of "near". Show a useful period and complete tracks from
the registered data. Narrow the region and time through the declared query
bindings; center or zoom the map only if its schema exposes that control.
Inspect the resulting view, select a candidate and inspect its real records.
Check distance and time numerically before naming the storm. A cropped track
or nearest-looking line alone cannot establish the answer. An annotated
location or track can make the explanation easy to inspect.

## Chart: is this relationship correlated or linear?

Inspect units, paired observations, missingness and grouping before choosing
encodings. Start with a scatter plot and a readable subset or aggregation
when overplotting obscures it. Change filters, groups or scales deliberately
and inspect each resulting view; retain counts and identify the shown slice.
Calculate the relevant correlation and fitted relationship from the data.
Compare a trend and residual view when the linear form matters. Distinguish
an apparent pattern from a numerical result and from any causal claim.
Mark an outlier or range to explain what the evidence changes.

## Pointing at a chart

The existing chart component can use a guarded Vega-Lite layered spec.
`examples/annotated-scatter.json` shows an unfilled red circle and label
around an existing observation using its stable ID, with all base rows kept.
Its names and rows are synthetic examples. Adapt fields and labels to the
actual data, preserve the named `source` data input, and run the catalog's
chart guard. The mark's size is a screen area; its center follows data scales.
This is not a generic canvas-overlay API.

## Dashboard: make a dense report useful

Review the overview at docked width, then each relevant tab and expanded
view. Look for repeated graphs, unreadable legends, clipped labels and views
that do not help a reader make the intended comparison. Reduce redundant
content, add concise interpretation, and put useful detail behind supported
navigation or optional-view controls. The agent chooses the presentation;
the person can reveal the detail. Inspect linked selections and shared
controls so a change remains meaningful across views. For a saved report,
retain source lineage and publish the corrected definition as a new version.
