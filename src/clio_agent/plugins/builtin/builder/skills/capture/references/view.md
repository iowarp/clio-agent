# View

How results should look when the person keeps asking for the same picture. First ask
who owns the data the view shows:

- **CLIO holds it**: the person's files and runs in the workspace, results CLIO
  computed, a reference table the plugin can carry. The view is an **A2UI surface**,
  this reference.
- **Another application or system owns it**: an observing program's data service, an
  instrument's controller, a lab system with its own records and actions. The view
  belongs with that system: an **MCP App** served by the MCP server that talks to it.
  See `references/app.md`.

## 1. Compose the catalog (no code)

The `clio-workspace` catalog draws the person's data from a registered file by
reference: `clio.chart.v1`, `clio.data-table.v1`, `clio.map.v1`, metrics, steps,
diagrams, code, diffs, callouts and layout (`Row`, `Column`, `Grid`, `Tabs`, `Card`).
Load `a2ui-catalog-clio-workspace` for their fields. A plugin captures the view as a
skill that says what to write, which component to use and how to configure it; the
agent calls `create_a2ui_surface` with it.

- Point components at the data with `dataUri` (a workspace path or `artifact://`), not
  inline rows. `artifact://<workspace>/<name>@latest` follows a file that is still
  growing, so the view updates as new versions are recorded.
- Every chart is `clio.chart.v1`: a `preset` or a Vega-Lite `spec`. A new kind of plot
  is a new `spec`, never a new component.
- Name columns with the `*Field` properties; the validator checks them against the
  file's real columns.

A plugin may also ship a catalog under `ai.iowarp.clio/a2ui/<name>/`, but it only gives
other names to widgets CLIO already renders (`implements.<Name>.kernel`); a kernel CLIO
does not have is refused with `a2ui_component_unimplemented`. It cannot add a widget.

## 2. A widget the catalog does not have: a change to CLIO

When the person needs a kind of widget CLIO does not draw at all (a periodic table
picker, a sequence viewer, a crystal lattice editor), it becomes a new A2UI component
in CLIO itself: its schema, its validator and its renderer change together, and every
plugin can then use it. No plugin can add one. Say so plainly and offer the way:

> This needs a new kind of view in CLIO itself, which a plugin cannot add. If you give
> me access to GitHub, I'll open a pull request with the new widget; it then waits for
> CLIO's maintainers to review and approve it before it reaches your app. Until then
> I'll show it with [the closest composition of existing widgets].

Do not open anything without that access and the person's yes. With it, follow the
`a2ui-component-design` guidance where it is available, open the pull request against
the CLIO repositories, give the person its link, and do not describe the widget as
available until it is merged and released. Meanwhile the plugin ships the closest
composition, so the person is not left waiting.

Do not reach for an MCP App instead: an MCP App is another system's view of its own
data, not a way around adding a widget to CLIO.

## Worked example

A person training language models asks every run "show me how it trained": loss for
training and validation over steps, and the learning rate beneath. The training script
already writes `train_log.csv` (`step`, `split`, `loss`, `lr`), so the plugin is one
skill that knows the file and the chart.

```json plugin.json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "training-curves",
  "version": "0.1.0",
  "description": "Shows how a model trained: training and validation loss over steps with the learning rate beneath, from the run's train_log.csv.",
  "author": {"name": "Clio"},
  "license": "BSD-3-Clause"
}
```

```markdown skills/training-curves/SKILL.md
---
name: training-curves
description: Show how a model trained, or how a training run is going ("how did it train?", "is it still improving?", "show the loss curves").
---

The run folder holds `train_log.csv` with columns `step`, `split` (`train` or `val`),
`loss` and `lr`, one row per logged step and split.

1. Check the columns and one row of the file before building the view.
2. Call `create_a2ui_surface` with this one component. Replace `WORKSPACE` with the
   workspace and `RUN` with the run folder; `@latest` makes the chart follow the file
   while the run is still writing it.

        {"id": "curves", "component": "clio.chart.v1",
         "dataUri": "artifact://WORKSPACE/RUN/train_log.csv@latest",
         "spec": {"vconcat": [
           {"mark": "line", "height": 260,
            "encoding": {"x": {"field": "step", "type": "quantitative"},
                         "y": {"field": "loss", "type": "quantitative", "scale": {"type": "log"}},
                         "color": {"field": "split", "type": "nominal"}}},
           {"mark": "line", "height": 120,
            "transform": [{"filter": "datum.split == 'train'"}],
            "encoding": {"x": {"field": "step", "type": "quantitative"},
                         "y": {"field": "lr", "type": "quantitative", "title": "learning rate"}}}]}}

3. Below the chart, say in one or two lines where validation loss bottomed out (its
   step and value) and whether it rose after that.

Never redraw the curves as a static image; the chart reads the file by reference.
```
