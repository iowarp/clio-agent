---
title: "Find a decision in research notes"
description: "Use a fictional field notebook to find a calibration decision, inspect its source and bring it into a conversation."
source: https://clio.iowarp.ai/docs/examples/research-notes/
---

# Find a decision in research notes

Use a fictional field notebook to find a calibration decision, inspect its source and bring it into a conversation.

> **Preview:** The knowledge-source controls (in development builds). Availability depends on your build.

Suppose you remember that your team discussed calibration before comparing two
sets of measurements, but you do not remember where the decision was written.
This example shows how to find the note and read the surrounding context.

## Open the example notebook

1. Download and extract the [fictional Field notebook](https://clio.iowarp.ai/examples/field-notebook.zip).
   It contains 62 linked notes and a small six-note **Field study** folder.
2. In your workspace, choose **+ → Attach → Obsidian vault**.
3. Select **Vault on this host**, choose the extracted **Field study** folder and
   keep **Read only**. Choose **Connect vault**.
4. Open the source's graph and select the **Calibration** note. Read it beside
   the graph, then follow its links to the surrounding decisions.

![Calibration selected in the fictional Field study notebook, with its linked notes and reader visible](plugin-asset://guide/captures/knowledge-graph.webp)
*Read the original note and its context. A graph line means the notes are linked; it does not prove that their scientific claims agree.*

## Ask a grounded question

Choose **Attach note** to put the relevant note in your draft. Then ask:

```text
What does this notebook say we must check about calibration before comparing
measurements? Link the notes behind your answer, distinguish decisions from
open questions, and keep the notebook unchanged.
```

Read the linked notes before accepting the answer. If CLIO gives a conclusion
without a source, ask it to identify the note and passage. An unresolved link or
missing record is a reason to qualify the result, not fill in a plausible answer.

## Search or broaden the context

Use the file list or source search when you know a phrase. Connect the whole
**Field notebook** instead of only **Field study** to explore its larger set of
connections. The selected folder is the access boundary; a link does not grant
access to a note outside it.

![The larger fictional notebook as linked groups of research notes](plugin-asset://guide/captures/knowledge-overview.webp)
*The complete example notebook makes related groups visible. Every note is fictional. Read the linked material before drawing a scientific conclusion.*

Continue with [Explore research notes](docs/files/knowledge.md) for connection,
search, reader and editing controls. For finding an earlier discussion in CLIO
itself, use [Search files and conversations](docs/search.md).
