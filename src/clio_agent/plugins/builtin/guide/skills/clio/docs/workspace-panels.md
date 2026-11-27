---
title: "Your workspace: conversation, data and results"
description: "Find your files, open a result beside the conversation and keep track of work that needs you."
source: https://clio.iowarp.ai/docs/workspace-panels/
---

# Your workspace: conversation, data and results

Find your files, open a result beside the conversation and keep track of work that needs you.

> **Preview:** The compact Data/Work layouts pictured here (in development builds). Availability depends on your build.

The conversation explains the work. The right-hand area lets you inspect its
files, ongoing operations, context, and results without leaving the conversation.
Start with the compact **Data** and **Work** summaries, then open full details
when you need more room.

![Compact Work summary with three open questions, three of five current todos and Show completed](plugin-asset://guide/captures/docs-compact-work.webp)
*The compact Work summary keeps unanswered questions and current todos beside the conversation. Use the open-view control in its header when you need the full Work tab.*

## The compact summaries

| Summary | Use it to find |
| --- | --- |
| Data | Context usage, connected sources, files, changes, and outputs associated with the conversation. |
| Work | Open questions, child agents, tool calls, background tasks, and current todos where available. Open the full Work view for the work inventory and long-running controls. |

Expand a section to inspect its items. Tool calls start collapsed. Your section
choices are remembered for the conversation, so reopening the summary restores
the sections you chose. Pending and in-progress todos stay visible; **Show
completed** opens their history.

Use **Open full details** to inspect the conversation's evidence in a larger
panel, or **Open Work view** for the full work inventory. Opening a larger panel
can temporarily make room by hiding the compact column. Closing it restores the
previous column layout and disclosure choices.

## The full detail views

The **Observability** tab contains four views:

| View | The question it helps answer |
| --- | --- |
| Evidence | What operations, sources, files, changes, and outputs were recorded? |
| Timeline | In what order did operations and interactions happen? |
| Gantt | Which operations overlapped, and how long did recorded work take? |
| Context | What context and files are available to the selected agent, and how much of its context window is used? |

These are views of recorded work. Missing timing or context information is not
proof that no work occurred; some connected services provide less detail.

![Observability tab with Evidence, Timeline, Gantt and Context navigation above registered bike-station artifacts](plugin-asset://guide/captures/docs-panel-evidence.webp)
*Choose Evidence to find registered outputs. Its navigation also opens Timeline, Gantt and Context. Expand Artifacts, search or filter the list, then select the file you want to inspect.*

Open a file or artifact to read it in its own tab. Where available, **Lineage**
shows declared input and output relationships. See [Check answers and sources](docs/evidence.md)
for the difference between a recorded relationship and a verified conclusion.

![Gantt view showing the recorded timing of a conversation family](plugin-asset://guide/captures/docs-conversation-family-gantt.webp)
*The Gantt view makes recorded timing and overlapping work easier to inspect. Open an item for its underlying detail.*

## Work is a separate tab

The full **Work** tab holds the work inventory: open questions, todos, goals,
loops, and schedules. Use it to answer a question, inspect limits, stop active
controls, or review previous work. It is separate from the Gantt timing view.

![Work selected beside Observability, with open questions and the current todo list](plugin-asset://guide/captures/docs-work-questions-todos.webp)
*The full Work tab keeps the same questions and checklist accessible when you open the larger panel. Observability remains a separate tab beside it.*

For background task status and child agents, start with the compact Work summary
or Evidence. Open a child's conversation to inspect its assignment and returned
work. “Turn ended” tells you the child stopped; its result tells you what it
accomplished.

## Other tabs open from what you select

Files, reports, dashboards, source browsers, changes, child conversations, and
terminals can open as separate tabs when you select the corresponding item.
The available tabs depend on the workspace and the connected service.

Selecting a file opens a preview; it does not by itself attach that file to a
new message. Use [attachments or references](docs/files/index.md) to give CLIO file
context. Reading a tool result or switching detail views does not start a new
agent operation.

![morning-findings.md open in its own tab beside Observability and Work, with Preview, Versions and Lineage controls](plugin-asset://guide/captures/docs-panel-file-preview.webp)
*Selecting morning-findings.md opens its preview in a new tab. Preview reads the document; Versions and Lineage inspect its history and declared relationships. The Work and Observability tabs remain available.*

- [Follow work in progress](docs/following-work.md): Distinguish execution, checklist status, and returned evidence.
- [Answering questions](docs/questions.md): Find and answer the questions still open in this conversation.
- [Open and edit documents](docs/working-with-files.md): Preview, edit, and reference documents in the workspace.
