---
title: "Save and share your work"
description: "Download a file, export a report or share a conversation with its recorded evidence."
source: https://clio.iowarp.ai/docs/exports/
---

# Save and share your work

Download a file, export a report or share a conversation with its recorded evidence.

Choose what your colleague needs: one file, an interactive report, or the
conversation and evidence behind the result. You can inspect every saved result
before sending it.

## Choose what to send

| Your colleague needs | Choose | What travels with it |
| --- | --- | --- |
| An editable output or original dataset | **Download file** in its viewer. | That file's bytes, such as CSV, DOCX, or an image. |
| A figure for a slide or draft | **PNG image** in a supported visual's download menu. | The rendered image; no interactive controls. |
| A dashboard to explore | **HTML dashboard** in the dashboard viewer. | The report, captured data, and supported local interactions. |
| Your questions, answers, and recorded work | Session **Download HTML**. | One readable review file with its viewer resources. |
| The conversation and its session files | **Artifacts**, then **Download ZIP**. | The review, recorded traces, and captured session inputs and outputs. |
| The investigation and the current workspace | **Workspace files**, then **Download ZIP**. | The session export plus registered workspace files as they exist now. |

For the dashboard's exact buttons and actual output files,
[see the dashboard export walkthrough](docs/dashboards.md#find-the-export-buttons).

## Export the conversation

> **Preview:** The named session-export checkboxes and download controls (in development builds). Availability depends on your build.

1. Open the conversation's **⋯** menu at the upper right, or the conversation's
   menu in the left sidebar. Choose **Export**.

2. For a review of the conversation, leave both checkboxes clear and choose
   **Download HTML**.

3. Open the downloaded HTML in your browser. The recipient can read it without
   CLIO installed or running.

![Conversation menu with Export expanded, the Artifacts and Workspace files checkboxes clear, and Download HTML available](plugin-asset://guide/captures/docs-session-export-menu.webp)
*The conversation's ⋯ menu opens the export choices. With both boxes clear, Download HTML saves one review file. The transcript is always included.*

*Recording on the site: Export the conversation. Open the saved review..* Download an HTML review from the session menu, then open the saved conversation and activity in your browser.

## Include the files as well

Check **Artifacts** when the recipient needs the files this
session and its child agents read or produced. The action changes to
**Download ZIP**. This does not collect every other session's outputs.

![Export submenu with Artifacts checked and Download ZIP available](plugin-asset://guide/captures/docs-session-export-zip.webp)
*Checking Artifacts changes HTML to ZIP. Workspace files adds the current registered workspace and also includes session artifacts.*

Use **Include workspace files** when your colleague also needs the broader
working directory. It includes the current files, including work from other
sessions in that workspace, and can make the archive substantially larger.

Send the ZIP, then ask the recipient to **extract it and open index.html**.
Keep the extracted folder together: its file links depend on that structure.

## Inspect what your colleague receives

![Downloaded session review open in a browser with the recorded fictional bike-count conversation](plugin-asset://guide/captures/docs-session-review.webp)
*The saved review opens offline with the conversation and recorded activity. Expand an activity row to inspect its inputs and results.*

| In the saved review | What to check |
| --- | --- |
| **Conversation & activity** | The question, answer, and recorded tool inputs and results. Expand activity to inspect what actually ran. |
| **Files**, when files are included | Which inputs and outputs were included. Editable documents and their previews remain separate files. |
| **Evidence & skills** | Recording coverage, captured instructions, excluded files, and uncaptured dependencies. |
| Saved views | Whether the supported chart, table, or other view contains the records your colleague needs. |

A text-only exchange may not have generated artifacts. Including artifacts does
not invent files that the session never produced. Missing historical versions,
skill instructions, or visual dependencies are reported explicitly.

A saved review does not rerun tools, recreate external services, or rebuild the
execution environment. Importing a transcript into CLIO restores recorded
history; it does not automatically restore the workspace or execute past actions.

## Find your download

In a browser, use the browser's downloads controls. In the development Windows
desktop app, the header's **Downloads** button opens the native download history;
on macOS and Linux it opens the Downloads folder. Saving a file does not
automatically launch its associated application.

If you are sharing one report instead of the investigation, follow
[Dashboards](docs/dashboards.md#find-the-export-buttons). To inspect the evidence
before exporting, open [Check answers and sources](docs/evidence.md).
