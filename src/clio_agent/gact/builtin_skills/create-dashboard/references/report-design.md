# Compose an explained report

Start with a question and evidence, not a list of available components. Write
one sentence describing the supported finding and what remains uncertain.
Compose related evidence in one initial space. Keep computations and source
references available. A static export should contain the main explanation
without requiring hidden tabs to make sense of it.

## Give the reader a path

- Lead with a short finding, scope and time period using Text. Use a few metric
  components for quantities that support the finding; retain units and bases.
- Give the central comparison the most space. Use a Row of Frames or Columns
  with weight 2 and weight 1 for a wide chart beside a narrower comparison.
  Put another Row beneath with weights 1.5 and 1.5 for an equal map/image and
  table/explanation pair. A 2:1 row followed by a 1:1 row is supported today.
- Use Grid for equal KPI cards. Frame gives each analysis its own title and
  context. Use space for readable evidence, not repetitive borders and prose.
- Keep the images, viewports and explanations that interpret the same object
  together. For a research report, place material images and parameters beside
  linked 3D views and their measured result distributions. Keep a shared object
  identifier and put relevant selectors next to the views they affect.
- Prefer one overview with complementary views visible together. Tabs are for
  independent questions or optional depth, not a default way to split related
  comparisons. Important contrary evidence stays visible with the finding.
- At a narrow dock, stack analytical rows rather than squeezing plots. At wide
  size, retain authored proportions. Inspect both; do not make the expanded
  report a single long column merely because the dock is narrow.

Example structure (replace child Text with actual catalog views):

```json
[
  {"id":"root","component":"Column","children":["mainRow","detailRow"]},
  {"id":"mainRow","component":"Row","children":["trend","balance"]},
  {"id":"trend","component":"Frame","weight":2,"title":"Main comparison","child":"a"},
  {"id":"balance","component":"Frame","weight":1,"title":"Supporting comparison","child":"b"},
  {"id":"detailRow","component":"Row","children":["image","evidence"]},
  {"id":"image","component":"Frame","weight":1.5,"title":"Object and context","child":"c"},
  {"id":"evidence","component":"Frame","weight":1.5,"title":"Measurements","child":"d"},
  {"id":"a","component":"Text","text":"Replace with the principal chart."},
  {"id":"b","component":"Text","text":"Replace with its comparison."},
  {"id":"c","component":"Text","text":"Replace with an image, map or viewport."},
  {"id":"d","component":"Text","text":"Replace with related evidence and explanation."}
]
```

## Make encodings carry meaning

Judge the report as one explanation. A collection of independently attractive
panels can still leave the reader to discover the relationship. Give each view
a distinct job in the same question: establish the pattern, locate it, explain
its timing, and expose the supporting measurements. Omit a panel that adds no
useful evidence. Keep the main comparison visible before optional detail.

Use a clear hierarchy: a finding-led title, a few readable measures, concise
panel headings, and quieter scope/method text. Avoid repeating the same heading
in a Frame and its chart. Use direct labels and restrained annotations for the
important exception or threshold. Leave ordinary context muted; decorative
colour on every card competes with colours that actually encode data.
For charts and maps, put the useful title on the view itself and leave the
surrounding Frame untitled. Removing the view's title instead exposes its
generic fallback, such as "Chart" or "Locations", beneath the Frame heading.

Choose a small set of consistent category meanings across views. Label the
categories; colour alone is insufficient. Automatic nominal chart/map colours
use the same category identity across tabs and when a subset changes. Keep
category labels identical across related data. The finite palette can repeat;
retain labels, filter dense groups and review the actual marks. If authoring
an explicit chart scale, use a complete domain/range consistently in every
related authored chart; do not imply that an unrelated map adopts that scale.

Make interaction extend the explanation: selecting an object should reveal its
matching measurements and location; put its identifying image and context next
to those views. Preserve stable keys so selection survives sorting/filtering.
Keep the initial report understandable as a static image, then demonstrate one
useful linked interaction. Interactivity does not compensate for an unclear
initial view, and an attractive overview does not prove its links work.
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
Keep the occupancy chart, station balance and useful map visible together,
with a compact linked table alongside. Retain all snapshots below or in optional
depth. Do not infer individual trips from dock counts alone. A rider filter should affect the related
views together, rather than changing only a headline. An animation is useful
only if the temporal progression is part of the explanation.

**Which design improved stiffness without excessive mass?** Show the baseline
once. Put the two useful evolutions and measured mass/stiffness comparisons
next to one another; link the real design identifiers and deliberately share
mesh camera controls when comparable views should rotate together. Keep
simulation assumptions and uncertainty visible. A mesh's colouring does not
establish a quantitative improvement without measured result fields.

**Is the response approximately linear, and where does it depart?** Lead with
the fitted relationship and its interval of applicability. Show the points,
fit and supported uncertainty; directly label the important departure. Put
residuals beside the fit and keep exact rows available below. Use a group
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
