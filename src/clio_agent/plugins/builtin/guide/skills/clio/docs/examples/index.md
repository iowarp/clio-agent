---
title: "Worked examples"
description: "Choose a complete investigation with a starting point, a request to try and checks for the result."
source: https://clio.iowarp.ai/docs/examples/
---

# Worked examples

Choose a complete investigation with a starting point, a request to try and checks for the result.

Choose a small task that resembles your own work. Each example explains what to
bring, what to ask, what a useful result looks like and how to check it.
Model wording, tool choices and layouts can vary.

- [Check a small dataset](docs/examples/check-a-dataset.md): A first analysis with a downloadable fictional CSV. Inspect the structure, ask one question and check the evidence.
- [Explore ground motion](docs/examples/ground-motion.md): Use public EarthScope data, choose a station and ask for its position over time. Requires the EarthScope agent and internet access.
- [Analyze EarthScope data from NDP](docs/examples/ndp-earthscope.md): Choose a real station CSV, inspect time coverage, chart its components and resume a finite stream sample. Requires CLIO v0.9.5-beta.8 or later.
- [Explore a hurricane in HDF5 data](docs/examples/hdf5-paraview.md): Track Hurricane Helene in public ERA5 files, see its clouds in ParaView and share a time-linked dashboard. Requires ParaView 6 and about 5 GB of data.
- [Follow a branched study](docs/examples/branched-study.md): A recorded study asked as a plain question: competing strategies tested in parallel branches, with every claim traced to its games.
- [Train a hybrid quantum classifier](docs/examples/hybrid-qml.md): A recorded session with the IBM Quantum plugin: a hybrid model and a classical baseline, then what a noisy device does to each prediction.
- [Research a question in depth](docs/examples/deep-research.md): Follow one deep research run on lossy compression for HDF5: named researchers, quote and critic checks, the coordinator and a sourced report. Requires web or literature search.
- [Find a decision in research notes](docs/examples/research-notes.md): Open a fictional field notebook, trace a calibration decision and attach the relevant notes. Requires the development knowledge-source feature.
- [Create a report and presentation](docs/examples/reports-and-slides.md): Use downloadable fictional sensor notes, then review and revise real recorded Word and PowerPoint outputs.

## Before you start

Have a working [CLIO installation](docs/install.md) and [model connection](docs/connect-a-model.md).
Use a separate project folder for an example. Keep your original research data
outside the example until you are comfortable with the workflow.

The examples use public or explicitly fictional material. Recordings and screenshots
identify their source; an illustrative screenshot is not a promise that the model
will produce an identical answer. [Features and versions](docs/versions.md) explains
which controls require a development build.

Once you have a result, continue to [check its sources](docs/evidence.md),
[make a specific revision](docs/working-with-files.md) or [save and share it](docs/exports.md).
