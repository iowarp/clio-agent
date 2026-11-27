---
title: "Create a report and presentation"
description: "Use fictional sensor notes to generate, review and revise editable documents."
source: https://clio.iowarp.ai/docs/examples/reports-and-slides/
---

# Create a report and presentation

Use fictional sensor notes to generate, review and revise editable documents.

Describe the audience, the material to use and the result you need. Review the
finished file, then ask for changes in the same conversation. For help with
opening previews and downloading files, see
[Open and edit documents](docs/working-with-files.md).

## Generate a report

These two recordings start in fresh conversations with source notes and a
summary of fictional sensor readings. CLIO creates the files during the recorded
runs. Each clip shows the request, agent activity and file arrival, followed by a
brief look at the result.

Put the [sample notes](https://clio.iowarp.ai/media/document-generation/sensor-notes.md) and
[sample summary](https://clio.iowarp.ai/media/document-generation/sensor-summary.json) in a `sources`
folder in your workspace, then ask:

> Use the sensor notes and summaries in sources/ to create a polished two-page Word report for our team meeting. Include a comparison chart, the limitations and next steps. Save the editable report and a PDF preview. Clearly label the sample data fictional.

*Recording on the site: From a request to a generated Word report.* Send a prompt, watch CLIO build and review the report, then open the newly generated file.

Download the actual recorded output:
[Word report](https://clio.iowarp.ai/media/document-generation/Sensor_Summary_Team_Report.docx) ·
[Report PDF](https://clio.iowarp.ai/media/document-generation/Sensor_Summary_Team_Report.pdf).

## Generate a presentation

Use the same source files and describe the audience, length and content you need:

> Use the sensor notes and summaries in sources/ to create a six-slide team briefing. Include a comparison chart, key limitations and next steps, with speaker notes. Save an editable PowerPoint and a PDF preview. Clearly label the sample data fictional.

*Recording on the site: From a request to a generated PowerPoint.* Watch CLIO create the deck, respond to a layout correction, and publish the corrected PowerPoint and PDF.

The first draft needed better cover contrast and more space below the final
heading. The recording includes the follow-up request and CLIO's correction.
Download the corrected [PowerPoint](https://clio.iowarp.ai/media/document-generation/sensor_team_briefing.pptx)
and [Slides PDF](https://clio.iowarp.ai/media/document-generation/sensor_team_briefing.pdf).

Open the result and check the wording, tables, figures and layout. Ask for any
corrections, then review the updated version before sharing it. Download the
editable document if you want to continue in Word or PowerPoint.

## More examples

<details>
<summary>Revise an existing report or presentation</summary>

These recordings use fictional sensor observations. Download a reference file
below, add it to a message, and adapt the request to your own work.

| Example | Starting file | Revised file | Revised preview |
| --- | --- | --- | --- |
| Two-page evidence report | [Word report](https://clio.iowarp.ai/media/document-reference/reference-report.docx) | [Revised Word report](https://clio.iowarp.ai/media/document-reference/revised-report.docx) | [Report PDF](https://clio.iowarp.ai/media/document-reference/revised-report.pdf) |
| Six-slide briefing | [PowerPoint deck](https://clio.iowarp.ai/media/document-reference/reference-deck.pptx) | [Revised PowerPoint deck](https://clio.iowarp.ai/media/document-reference/revised-deck.pptx) | [Slides PDF](https://clio.iowarp.ai/media/document-reference/revised-deck.pdf) |

### Revise a report

Say what should change and what should stay. This request updates the report's
recommendations while keeping its existing structure:

> Make the next steps request calibration records and a review of the raw readings. Keep the headings, tables, figure and theme. Save a revised Word document and PDF preview, and preserve the original.

*Recording on the site: Make a revision and review the result.* Reference a report, revise its recommendations, then review the Word artifact and saved PDF.

### Revise a presentation

Reference the deck and describe the change. For example:

> Revise the final recommendations and speaker notes to make clear which findings still need calibration and raw readings. Keep the theme, charts and editable tables. Save a revised PowerPoint file and PDF preview.

*Recording on the site: Revise and review a themed presentation.* Revise a six-slide briefing, correct formatting after review, and inspect its PDF preview. Charts and tables stay editable.

The revised downloads include the corrections shown in the recordings. Compare
them with the starting files to see how the text and layout changed.

<details>
<summary>Compare the starting report and presentation layouts</summary>

![Both pages of the fictional reference report](https://clio.iowarp.ai/media/document-reference/report-overview.png)

![Six slides of the fictional reference presentation](https://clio.iowarp.ai/media/document-reference/deck-overview.png)

</details>

<details>
<summary>Source data and scripts for the fictional examples</summary>

The [fictional source summary](https://clio.iowarp.ai/media/document-reference/reference-source.json),
[report builder](https://clio.iowarp.ai/media/document-reference/create-reference-report.mjs) and
[presentation builder](https://clio.iowarp.ai/media/document-reference/create-reference-deck.mjs)
are available if you want to explore how the starting files were made. Their
PDFs are also available: [report](https://clio.iowarp.ai/media/document-reference/reference-report.pdf)
and [presentation](https://clio.iowarp.ai/media/document-reference/reference-deck.pdf).

</details>

</details>
