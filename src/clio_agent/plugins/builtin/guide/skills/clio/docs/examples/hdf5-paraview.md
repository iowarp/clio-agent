---
title: "Explore a hurricane in HDF5 data"
description: "Use public ERA5 reanalysis of Hurricane Helene to inspect HDF5 files, track the storm, see its clouds in ParaView and share a time-linked dashboard."
source: https://clio.iowarp.ai/docs/examples/hdf5-paraview/
---

# Explore a hurricane in HDF5 data

Use public ERA5 reanalysis of Hurricane Helene to inspect HDF5 files, track the storm, see its clouds in ParaView and share a time-linked dashboard.

> **Preview:** The HDF5 file viewer, the ParaView plugin and time-linked dashboards (since v0.9.7). Availability depends on your build.

Hurricane Helene crossed the Gulf of Mexico on 24–27 September 2024 and came ashore
in Florida's Big Bend. This example works on ERA5 reanalysis of those days, stored as
NetCDF-4/HDF5 files, the way a person would: reference a file, look at a field, ask
for the storm's track and intensity, see its clouds in 3D, then share the result.

Every screenshot on this page is from one recorded conversation. Your run will cut
the data, word its answers and draw the 3D view in its own way.

## What to bring

- A working [CLIO installation](docs/install.md) and a
  [model connection](docs/connect-a-model.md) that can use tools. The run shown here
  used Claude Sonnet.
- ParaView 6.0 or later (`pvpython`) on your computer or on a machine you reach over
  SSH. The [ParaView plugin](docs/connectors.md#paraview) explains the three optional
  settings, such as a **Command to run first** that puts `pvpython` on the path.
- About 5 GB of disk for the data, and a folder to use as the workspace.

### Get the data

The files are ERA5 reanalysis from the public NSF NCAR bucket (`nsf-ncar-era5`), read
without an account:

- mean sea-level pressure and sea-surface temperature, hourly for September 2024;
- cloud liquid water, cloud ice, rain and snow on 37 pressure levels, hourly for
  24–27 September.

From a CLIO source checkout, the walkthrough's own script downloads them, 18 files and
about 4.7 GB, into the folder you give it:

```bash
uv run --frozen --no-sync python scripts/demo/helene/fetch_era5.py ~/helene/era5
```

Open the folder that holds `era5/` (here `~/helene`) as a workspace and start a
conversation with the **CLIO Data** agent. The recorded run used the **hdf5-ptc**
plugin in place of the agent's standard HDF5 plugin; it opens HDF5 and NetCDF-4 files
by handle for agents that work in scripts.

## What to ask

Each request is typed as a person would, without naming a tool or a step.

1. Type `@msl`, choose `msl_2024-09.nc`, and ask:

   ```text
   What's in here?
   ```

2. Open the field from the answer or from your message, look at it, and choose
   **Add to message** in the viewer. Then ask:

   ```text
   Hurricane Helene crossed the Gulf in late September 2024. Can you track its
   centre and central pressure in this from the 24th to the 27th?
   ```

3. Reference the sea-surface temperature field with `@sst`, then **Tab** into the
   file and choose `/SSTK`:

   ```text
   How fast did it deepen, and was the water under it warm enough to explain that?
   ```

4. Ask for the clouds. This request names ParaView and the look you want:

   ```text
   What did the clouds around it look like on the 26th, before it came ashore? The
   cloud, rain and snow water for those days are in the era5 folder. Show it to me
   in ParaView from above, tilted a little, so I can see the spiral. Give the cloud
   droplets, ice, rain and snow each their own strong colour, and hide the thin
   wisps so the shape of the storm stands out.
   ```

   If ParaView is not yet in the conversation, the agent offers it. Choose **Add**.

5. Ask for something to share, then for time to drive every part of it:

   ```text
   Put all of this on a dashboard so I can share it with colleagues.
   ```

   ```text
   Can I step through the days on this and have everything follow, the 3D clouds too?
   ```

   In this run the agent asked how often to step. The reply was
   `3-hourly, go ahead.`

6. Step the 3D view yourself, turn the storm, and ask what changed.

## What you get

### The file and its fields

The first answer listed the file's one variable, `MSL`, with its coordinates and time
range, and noted what did not match: attributes inherited from the global grid and a
fill value that stopped one description. Opening the field shows it in the file viewer.
At 26 September 12 UTC the storm is the low in the middle of the Gulf.

![The file viewer maximized on msl_2024-09.nc, dataset /MSL, 720 by 161 by 181 float32, drawn as an image at time step 612 with a deep pressure minimum in the middle of the grid](plugin-asset://guide/captures/field-msl.webp)
*Mean sea-level pressure at 26 Sep 12 UTC (time step 612 of 720) in the file viewer, with the y axis flipped so north is up.*

### Track and intensity

The track answer gave the pressure minimum hour by hour in a table, then what it
shows: a weak, broad low in the northwest Caribbean on the 24th, steady deepening
through the Yucatán Channel, and the lowest value in the file, **963.7 hPa** at
27 September 01 UTC near 29.0° N, 84.0° W. Its caveats came with it: ERA5 smooths
intense cores, the centre is the nearest grid point, and the track was not compared
with the official best track.

![The end of the track answer, with the hourly table, What it shows and Caveats, beside the pressure field; the composer holds the next question with the reference @sst_2024-09.nc › /SSTK](plugin-asset://guide/captures/track-answer.webp)
*The track answer beside the field. The next question already references the sea-surface temperature field.*

Asked about deepening, the agent measured about 40 hPa in three days and sea-surface
temperatures of 29.4–30.8 °C under the centre, 3–4 °C above the usual threshold for
tropical cyclones. It also said what the field cannot tell: the ocean's heat content
below the surface, and the cold wake the storm itself left.

![The deepening answer: a table of sea-surface temperature at the centre and in the box for each day, notes on the threshold, margin and trend, and a Limits of this answer section](plugin-asset://guide/captures/deepen-answer.webp)
*Warm water all the way, with the answer's own limits listed after it.*

### The clouds in ParaView

For the clouds, the agent cut the four water fields to the storm's region, wrote
them as a 3D series and drew a surface for each kind of water in ParaView: cyan cloud
droplets, magenta ice, blue rain and yellow snow, with thinner amounts hidden. The view
opens beside the conversation and keeps its own controls, such as the time slider.

![The agent's answer, with a table of each species' colour and threshold, What the render shows and What isn't good yet, beside a ParaView tab with coloured cloud surfaces around Helene on 26 September](plugin-asset://guide/captures/clouds-first-view.webp)
*The first view of the 26th. The answer says what still needs work, such as rain hidden under the ice and snow.*

### A dashboard to share

The dashboard put the headline numbers, the track on a map, the pressure and
sea-surface temperature curves, the 3D view and an hourly table on one page, with a
**Read with care** note. After the follow-up it holds all four days at 3-hourly steps.
Stepping the 3D view moves the selection on the map, the charts and the table to the
same time. The link is built to work the other way too, from a point on the track or a
curve to the 3D view; this run did not confirm that direction.

![A dashboard titled Hurricane Helene, 24-27 Sep 2024: four headline numbers, the hourly track on a map with one point selected, and central pressure and sea-surface temperature curves with the same time highlighted](plugin-asset://guide/captures/dashboard.webp)
*The shared dashboard. The highlighted point on the map and both curves is the time shown in the 3D view.*

![The same dashboard scrolled to its ParaView view at time step 23 of 32, labelled time linked both ways, beside the dashboard's description of the clouds](plugin-asset://guide/captures/dashboard-3d-linked.webp)
*The dashboard's live 3D view, at step 23 of 32 (26 Sep 18 UTC).*

The 3D view on its own, from above and tilted, shows the spiral of ice and snow around
the centre, an ice plume to the northeast, and two separate systems in the same box.

![The ParaView view maximized: orange snow and magenta ice surfaces curling around Helene's centre, blue rain beneath, cyan cloud droplets scattered around, and two separate cloud masses at lower left and lower right](plugin-asset://guide/captures/paraview.webp)
*Cloud, ice, rain and snow water on 26 Sep 18 UTC, as the agent left the view.*

## Check the result

- **Open the field at the same time.** The time slider in the file viewer finds the
  minimum the agent reports, and the attributes show the units and scaling.
- **Read the caveats.** ERA5 is a 0.25° reanalysis, about 28 km between grid points.
  Its central pressures are shallower than the real storm's, and the deepening is a
  lower bound. Compare with the National Hurricane Center's best track before you
  quote an intensity or a position.
- **Ask for a measurement, not a description.** The agent compared the clouds of two
  days by eye and said so. Ask it to measure, for example the ice and snow within a
  fixed radius of the centre.
- **Step the dashboard.** Choose a point on the track and check that the 3D view, the
  curves and the table move to that time.

## Open the dashboard and the conversation

The dashboard was saved with its own **HTML dashboard** download. It opens on its own
in a browser, without CLIO, with its data, map, charts and table. The 3D view needs
ParaView running, so the saved file shows its recording instead.

- [Open the Helene dashboard](https://clio.iowarp.ai/conversations/hdf5/helene-dashboard/): The shared dashboard from this run, with the 3D cloud view's recording. 21 MB.

The conversation itself was exported too, with the conversation menu's **Export**,
then **Download HTML**. The file opens on its own in a browser with every request,
answer and tool call, every result the agent read, including four reads of a whole
field of 2.8 million values each, and the dashboard under **Results**. The original
export stored each of those results twice and came to 110 MB; the published copy was
rebuilt from it by a newer CLIO that stores each result once.

- [Open the Helene conversation](https://clio.iowarp.ai/conversations/hdf5/helene-conversation/): The recorded conversation: each request and answer, the tool calls and their results, the loaded skills and the dashboard. 65 MB.

The picture below is the exported conversation opened from a file.

![The exported conversation open in a browser, Results tab: the dashboard's cloud section shows an eight-second recording of the 3D series with a play button, and the note that this is the live view's recording](plugin-asset://guide/captures/export-offline-recording.webp)
*The exported conversation opened from a file. In place of the live 3D view, the recording steps through the four days.*

## What this recording shows

- **One run, not a rate.** The requests are plain, so the run shows the agent's own
  choices of tools, cuts and views. It is one run; the 3D look varies from run to run.
- **The clouds request is directed.** It names ParaView and the look, so it shows how
  the view was made, not whether the agent would choose it.
- **The person's actions are scripted.** Opening the field, touring the dashboard,
  stepping and turning the 3D view were done by the recording script. Turning the view
  lost the storm, so a last request asked the agent to put it back, from above and
  tilted, on the 26th at 18 UTC. The 3D captures show the view after that request.
- **Linked in both directions, checked in one.** In this run the agent confirmed that
  stepping the 3D view moves the other panels. It reported that it could not confirm
  the other direction, from the panels to the 3D view, before its retries ran out.
- **A smaller copy of the same files.** To fit the recording machine's disk, each file
  was cropped to 5–45° N, 105–60° W with all variables and attributes kept. The agent
  noticed that the grid attribute still describes the global grid.
- **The data's resolution limits the view.** At 0.25° the storm's inner core is
  smoothed. Fine eyewall structure, as in kilometre-scale model output, is not in
  these fields.

> **Tip**
>
> To see how the same questions perform as direct tool calls or as scripts, and from a
> local file or from S3, read the
> [HDF5 benchmark](https://github.com/iowarp/clio-agent/blob/develop/docs/demos/helene-hdf5.md#benchmark-tool-calls-against-scripts-local-file-against-s3)
> in the walkthrough's notes.

## Related

- [Apps in a conversation](docs/apps.md)
- [ParaView settings](docs/connectors.md#paraview)
- [Dashboards](docs/dashboards.md)
- [Save and share your work](docs/exports.md)
