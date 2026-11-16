---
title: "Compare alternative answers"
description: "Compare independent drafts with Best of N, check the criteria, and choose one to continue or improve with feedback."
source: https://clio.iowarp.ai/docs/best-of-n/
---

# Compare alternative answers

Compare independent drafts with Best of N, check the criteria, and choose one to continue or improve with feedback.

Most questions need one answer. Ask for alternatives when the **approach or
wording is a choice**: a report introduction, an explanation for a different
audience, or competing analysis plans. **Best of N** runs independent attempts
at the same task, then lets you or the model choose one.

You can return to this workflow after [your first analysis](docs/examples/ground-motion.md).
It is an optional way to improve a result, not a step required to use CLIO.

> **Preview:** The independent draft tabs and evaluation controls (in development builds). Availability depends on your build.

## Compare three introductions

The finding is already known: the station snapshots total 54 bikes, Hill loses
37 while Downtown gains 37 between 6 and 9 am, and Fernhill is first recorded
empty at 8:15. Now choose a clear way to introduce that finding.

```text
Use Best of N: 3 candidates, one 2-sentence introduction each.
Facts: fictional bike counts total 54; Hill -37, Downtown +37, 6–9 am;
Fernhill empty at 8:15. No trip inference. Let me choose.
```

1. Send the request. Look for **Alternative drafts**, rather than a numbered
   list in an ordinary answer. Each **Draft** tab is one independent attempt.

2. Read the candidates. Open **Evaluation criteria** to check what they were
   asked to preserve. Reasoning and tool activity can be expanded separately.

3. Select **Pick Draft** on the candidate you want. CLIO continues the conversation
   from that candidate; the other attempts remain in the recorded run.

![Alternative drafts with three candidate tabs, evaluation criteria, and the Pick Draft control](plugin-asset://guide/captures/docs-best-of-n.webp)
*Switch tabs to compare the complete drafts. Pick Draft chooses the currently displayed candidate.*

*Recording on the site: Compare the drafts. Check the criteria. Pick one..* A real Codex/Luna run. The request, independent candidates, evaluation criteria, and the selection are recorded through the product controls.

The prompt can request the workflow, but the agent chooses whether to call the
draft tool. The presence of the draft block establishes that separate attempts
were run. The default maximum is eight; your deployment can set a lower limit.

## Choose who judges

In **Settings → Fleet → Best of N and refinement**, choose the model that scores
candidate answers and refinements. It defaults to the conversation's model.
This changes the judge; candidate generation still uses the conversation's model.

**You choose**

Say **“let me choose.”** CLIO waits for your selection. Compare the drafts against
the source, not just against each other. In this example, an introduction should
retain the interval and counts without claiming that individual journeys were
observed.
**The model chooses**

Say **“choose the clearest introduction that preserves these facts.”** Give
specific criteria, such as plain language, accurate units, and a word limit.
Evaluated candidates receive a score from 0 to 1; **Selected** identifies the
chosen draft.

A score is the model's judgment against the criteria. It does not establish
factual correctness or replace checking the original data.

## Improve one draft with feedback

Use **refinement** when you already know what needs to change. Each new attempt
uses feedback from the preceding one, instead of starting independently.

```text
Use user-judged refinement, up to 3 drafts, one at a time. Write one
2-sentence report introduction. Facts: fictional station snapshots total
54 bikes; Hill -37, Downtown +37 from 6–9 am; Fernhill first empty at 8:15.
Start with concise technical wording. Let me comment before accepting.
```

1. Read **Draft 1**. Unlike Best of N, refinement starts with one candidate.

2. Enter feedback in **Comment for Draft 1**. The button changes from
   **Pick Draft 1** to **Refine Draft 1** when you add a comment.

   ```text
   For a nontechnical colleague: use plain language, keep all figures,
   and say these snapshots do not prove individual bike journeys.
   ```

3. Click **Refine Draft 1**. Read **Draft 2**, marked **Refined from Draft 1**,
   and check whether it followed the feedback. Earlier drafts remain in their tabs.

4. To accept the revision, leave its comment field empty and click **Pick Draft 2**.
   To revise again within the run's limit, enter another comment first.

![User-judged refinement with feedback entered and the Refine Draft 1 button ready](plugin-asset://guide/captures/docs-refinement.webp)
*Feedback changes the action from accepting the draft to refining it. The next candidate is based on this draft and comment, rather than an independent alternative.*

*Recording on the site: Comment on a draft. Inspect the revision. Accept it..* Watch the feedback change the action to Refine Draft, then inspect the revised candidate and accept it without another comment.

![Draft 2 marked Refined from Draft 1, with revised plain-language wording and the Pick Draft 2 control](plugin-asset://guide/captures/docs-refinement-revised.webp)
*The actual revision retains the counts and adds that the snapshots do not prove individual journeys. The empty comment field makes Pick Draft 2 accept it.*

| Use | When it helps |
| --- | --- |
| **Best of N** | You want to compare distinct approaches to the same task. |
| **Refine** | You want to correct or improve one approach with specific feedback. |
| An ordinary follow-up | You only need a small change and do not need a formal comparison. |

Several attempts use more model work than one answer. Start with a small number,
and inspect their activity and session usage before requesting more.

## Continue with the chosen result

Tell CLIO where the selected introduction belongs: revise a document, use it in a
[dashboard](docs/dashboards.md), or develop the chosen plan. Selection does not
independently approve a protected change or publish work outside the workspace.

[Save and share your work](docs/exports.md) explains how to share the recorded
conversation. [Check answers and sources](docs/evidence.md) shows where to
inspect its inputs, artifacts, and history.
