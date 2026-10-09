# Compose an explained report

Start with a question and evidence, not a list of available components. Write
one sentence describing the supported finding and what remains uncertain. Use
that sentence to decide which views belong in the overview and which belong
in a detail tab. Keep computations and source references available.

## Give the reader a path

- Lead with a short finding, scope and time period using Text. Use a few metric
  components for quantities that support the finding; retain units and bases.
- Give the central comparison the most space. A Column with a full-width chart
  often reads better in a docked panel than several small plots in a Row.
- Use Grid for a modest group of metrics or complementary comparisons. Use
  Frame for a section with a useful title or explanation. Inspect the actual
  wrapped layout; component acceptance does not prove responsive composition.
- Put genuinely different analytical questions in Tabs. Keep the main answer
  visible in the initial tab. Bind activeTab when agent review needs navigation.
- Keep exact records, definitions, sources and additional series in an evidence
  tab or supported filters. Do not hide disconfirming evidence.

## Make encodings carry meaning

Choose a small set of consistent category meanings across views. Label the
categories; colour alone is insufficient. Automatic nominal chart/map colours
use the same category identity across tabs and when a subset changes. Keep
category labels identical across related data. The finite palette can repeat;
retain labels, filter dense groups and review the actual marks. If authoring
an explicit chart scale, use a complete domain/range consistently in every
related authored chart; do not imply that an unrelated map adopts that scale.
Use muted context and stronger emphasis for the comparison being explained.
Reserve selection highlights for the viewer's selection.

Use meaningful titles, short subtitles, direct series labels and units. Label
an important threshold, reference value, peak or exception when supported by
the data. A chart spec may layer a rule, text or outline over the named source;
do not add fabricated measurements to make an annotation. Keep comparison
axes and time intervals compatible. Do not truncate a bar baseline to make a
small difference appear large. State exclusions and aggregation intervals.

A preset is useful when it already explains the question. A guarded authored
Vega-Lite spec is appropriate when direct labels, reference bands, layers,
facets or semantic colour choices improve the explanation. Use the chart
skill's Altair exporter when helpful; all layers still read the component's
named source. The active catalog remains the authority for allowed shapes.

## Three worked compositions

**Why does a station run out of bikes in the morning?** Lead with the time it
runs empty and the observed inbound/outbound balance. Put dock occupancy over
the morning interval in the main chart, with the empty time labelled. Compare
net arrivals by station in a second chart using the same station identifiers.
Use a map only when location helps explain the imbalance. Keep trips and
station capacities in Evidence. A rider filter should affect the related
views together, rather than changing only a headline. An animation is useful
only if the temporal progression is part of the explanation.

**Which design improved stiffness without excessive mass?** Show the baseline
once. Put the two useful evolutions and measured mass/stiffness comparisons in
a Comparison tab; link the real design identifiers and deliberately share
mesh camera controls when comparable views should rotate together. Keep
simulation assumptions and uncertainty visible. A mesh's colouring does not
establish a quantitative improvement without measured result fields.

**Is the response approximately linear, and where does it depart?** Lead with
the fitted relationship and its interval of applicability. Show the points,
fit and supported uncertainty; directly label the important departure. Put
residuals in a complementary view and exact rows in Evidence. Use a group
selector instead of drawing every series by default. Visual alignment alone
does not establish causality or validate the fit.

## Close the loop on the authored result

Use inspect_a2ui_surface and capture_a2ui_surface on the actual viewer. Read
the returned image at docked width and inspect each relevant tab. Ask:

1. Can the reader identify the finding and its supporting comparison?
2. Are labels, legends, units and relevant marks legible without zooming?
3. Do linked views agree on categories, filters, dates and record identity?
4. Would fewer visible series, a different layout or a direct annotation help?

Correct a concrete defect, recapture and inspect the correction. Verify the
data behind claims separately. Stop when the report answers the question and
the useful detail is accessible. If capture is unavailable, state that limit.
Keep screenshots and intermediate versions as verification evidence; present
the latest requested deliverable once.

For a runnable composition using real catalog components, read
`references/bike-station-report.json`. Its clearly labelled synthetic example
connects morning occupancy, station balance, location and exact records to one
question. Use it as a composition pattern, never as evidence about a real bike
system. Replace its inline demonstration rows with the person's registered
dataset, retain stable station/category fields, and capture the actual result.
