---
title: "Your first analysis: check a small dataset"
description: "Bring a small CSV into CLIO, ask one answerable question and inspect the evidence before keeping the result."
source: https://clio.iowarp.ai/docs/examples/check-a-dataset/
---

# Your first analysis: check a small dataset

Bring a small CSV into CLIO, ask one answerable question and inspect the evidence before keeping the result.

Start with a question you can check by hand. This example uses **fictional bike
station counts**, a small table with measurements at several times. It requires
CLIO and a model that can use tools; it does not require a specialist agent.

Download [bike-stations.csv](https://clio.iowarp.ai/datasets/bike-stations.csv). The file is a teaching
example, not evidence about real stations or individual journeys.

## Add the data and ask one question

1. [Open CLIO and connect a model](docs/connect-a-model.md). Create a conversation
   in a project folder where you want to keep this example's outputs. Use the
   general-purpose agent already available in your installation.

2. Choose **+ → Attach** beside the message box and select the downloaded CSV.
   Check that the attachment is present before sending the request.
   [The attachment guide](docs/files/attach.md) explains copies and locations.

3. Send this request:

   ```text
   Inspect the attached CSV and explain what one row and each measurement column
   represent. Then tell me when Fernhill is first recorded empty. Show the source
   values behind your answer and identify anything the file cannot establish.
   Keep the original file unchanged. These counts are fictional.
   ```

4. Read any clarification before answering it. If CLIO asks to run a command or
   create a file, inspect that action and choose the permission you intend.
   **Allow once** applies to that action. You can deny it and ask for another approach.

5. Read the result and open the supporting file or table. A useful answer names
   the station, gives the observed time and shows which values support it.

![The message plus menu offers Attach and Reference](plugin-asset://guide/captures/guide-context-menu.webp)
*Attach brings in new material. Type @ when you want to refer to a file already available in CLIO.*

## Check the answer

In this sample, Fernhill has four bikes at 8:00 and zero at 8:15. Therefore 8:15
is the **first recorded empty observation**. The table does not tell you the
exact moment the count reached zero between observations or where the bikes went.

![A fictional station-count findings report showing its conclusion, limitations and supporting tables](plugin-asset://guide/captures/current-report.webp)
*An example of a reviewable result prepared through CLIO's tools. It uses the same fictional counts; it is an illustration, not a fresh model run or a required output layout.*

If your answer omits the source values or overstates what happened, send a
follow-up in the same conversation:

```text
Show the Fernhill values at 8:00 and 8:15. Distinguish the first recorded empty
observation from the exact time it became empty. Link the file or table you used.
```

Open **Data** to find the inputs and outputs, or expand a tool row to inspect the
recorded result. [Check answers and sources](docs/evidence.md) explains those views.
If CLIO cannot read the file, inspect its error and the selected attachment
before retrying. You do not need a particular Python script or a fixed output layout.

## Continue with your own work

Try another small dataset and ask CLIO to inspect its structure before making
assumptions. State the question, units you know, missing-information concerns and
the output you want. Ask it to explain unresolved fields rather than repair your
original measurements or silently discard inconvenient records.

You can then [build a dashboard](docs/dashboards.md) or
[create a report and presentation](docs/examples/reports-and-slides.md).
