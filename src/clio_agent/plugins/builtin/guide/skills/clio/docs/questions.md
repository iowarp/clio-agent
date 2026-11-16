---
title: "Answering CLIO's questions"
description: "See the different question types, add context to a choice, and find questions that still need your answer."
source: https://clio.iowarp.ai/docs/questions/
---

# Answering CLIO's questions

See the different question types, add context to a choice, and find questions that still need your answer.

> **Development preview:** these guides describe the task updates under review. Availability depends on your build.

When CLIO needs your judgment, it can ask a question in the conversation. Your
answer stays with that question and goes to the agent that asked it, including
when a delegated agent needs your input.

## See the different question types

Choose a tab or use the arrows to browse seven question types.

*An interactive example is shown on the site version of this page.*

CLIO's ordinary questions support text, a single choice and confirmations.
Connected tools can also request multiple selections, structured forms and browser
steps. Visual context can accompany a question; it is independent of the answer
format and whether the agent waits for you.

Several open questions are separate decisions. A form groups related fields into
one response; multiple selection lets you choose several items for one question.

## Keep a choice and add context

You do not need to choose **Something else** just to explain or amend one of
CLIO's suggestions. In choice, confirmation and multiple-choice questions, each
option has a **+** at its right edge, including before you have selected it.

1. Choose **+** beside the option you want. It selects that option and opens its
   comment field.
2. Write the clarification or change. For example, “Compare the groups, but show
   missing measurements before drawing conclusions.”
3. Check the selected option and comment, then choose **Send response**.

![Treatment groups selected with its comment open, a clarification about missing measurements, and a plus still available on Changes over time](plugin-asset://guide/captures/docs-question-comment-current.webp)
*The choice and its comment are sent together. The minus folds the comment field; switching choices preserves each draft.*

| What you mean | How to answer |
| --- | --- |
| “Option 1, with this clarification.” | Use **+** on that option and add the clarification. |
| “Option 2, but change this part.” | Use **+** on that option and describe the change. |
| “Neither option; take a different approach.” | Choose **Something else**, when offered, and write the alternative. |

For multiple selections, each selected option keeps its own comment. Comments
on options you leave unselected are not included in the submitted answer.
Switching choices or folding a field keeps its draft; selecting an option or
writing a comment does not submit it by itself.

Questions render emphasis, code, links and equations. Long prompts scroll within
the heading so you can reach the answer controls. Read the full question before
sending, especially when several decisions are open.

## Give a different answer

If none of the choices fits and **Something else** is offered, select it and
write your answer in **Your response**. Here, the answer asks CLIO to inspect
missing measurements before deciding which comparison to make.

![Something else selected with a written answer asking to inspect missing measurements before choosing a comparison](plugin-asset://guide/captures/docs-question-different-answer-current.webp)
*This proposes a different next step. To keep an offered option and add a condition, use that option's + instead.*

For a text question, type directly in **Your response**. Where **Answer in message
box** is offered, it moves your answer into the conversation's composer. Check
which question is selected before sending.

## Questions with images and interactive views

Questions linked to a dashboard leave it in its original view. The question
notice shows **Go to question**. Choose it to reveal the view and select the tab
containing its answer control. Your other tabs and entered values stay in place.
Buttons, filters and sliders alone do not make a dashboard a pending question;
the agent must explicitly ask. Small forms can explicitly appear inside a
question instead.

CLIO can put visual evidence beside a question. This is sometimes called an
**A2UI question**: the agent supplies an interactive view as part of the question.
You can ask in ordinary language:

```text
Compare these two plots. Show both images and ask me which one to keep, with space for a comment.
```

Other useful combinations include a chart with a choice of time ranges, a table
with checkboxes, or an image with a text field for your explanation. The layout
and available controls depend on the question and connected tool.

In the gallery's **Visual question** example, the chart shows morning changes at
fictional bike stations. Inspect it, choose **Hill stations** or **Downtown
stations**, add a comment if useful, then use **Send response**.

Changing a filter, zooming a plot or adjusting a field does not by itself answer
the question. Use its submit or answer control when ready. A completed chart or
dashboard can remain interactive after the question is answered.

Try the [choice picker](docs/widgets/choice-picker.md),
[checkbox](docs/widgets/check-box.md) and
[text field](docs/widgets/text-field.md) examples. To send a selection from a
finished view back to CLIO, see [Human-agent interaction](docs/widgets/interaction.md).

## Does CLIO wait for me?

> **Note: Task development preview**
>
> The task-based question flow below is on the development branch. Its
> screenshots show actual Codex/Luna and Claude/Haiku sessions with an isolated
> CLIO service. They do not establish completion of the wider Tasks
> qualification.

Asking a question leaves it open while CLIO can do independent work. When the
agent needs the answer, it explicitly waits on the question's task handle. An
answer already submitted is available immediately.

![A real Codex/Luna conversation remains active with Waiting for your answer inside its collapsed operations and one open question](plugin-asset://guide/captures/question-wait-desktop.webp)
*Waiting stays inside the active operations. The conversation has not finished, and the question remains available above the composer.*

These labels distinguish ordinary questions from permission and connected-tool gates:

| Question status               | What happens                                                                                                                             |
| ----------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| **Answer needed to continue** | The agent that asked is waiting for your answer before continuing.                                                                       |
| **Answer when ready**         | The question stays open while independent work continues. Your answer reaches the next eligible model iteration, or wakes an idle agent. |

An answer from CLIO does not necessarily close an **Answer when ready** question.
Check the open question count when you return to a conversation. A waiting child
agent can also need your answer while other work continues.

You can make the dependency clear in your request:

```text
Ask me which region to analyze. While I decide, check the column names and missing values.
```

If the work depends on your answer, say so: “Wait for my choice before running the
analysis.” Stopping its wait leaves the question open. Closing the answer dialog
also leaves it open. To cancel a question, use its cancellation control in
**Work** and confirm the question you intend to close. Cancellation is different
from answering “No” or stopping the current turn.

![The unanswered dataset question in a phone-width dialog, with a response field, Send response, and Close](plugin-asset://guide/captures/question-answer-phone.webp)
*Answer the specific question when ready. Closing this dialog leaves the question open.*

<details>
<summary>Cancel an open question</summary>

![A real task cancellation dialog identifies the dataset question and offers Keep running and Cancel task](plugin-asset://guide/captures/question-cancel-confirmation-desktop.webp)
*Work identifies the question before cancellation. Keep running leaves it open.*

</details>

## Find an unanswered question

Hover over or open the question count near the conversation to see **Open questions**.
Each entry has a short preview, a control to show the full question, and actions
to answer or go to its location in the conversation. The list also appears in
the full **Work** view.

![Open questions list with three previews and the third expanded to show its full prompt and Read and answer button](plugin-asset://guide/captures/docs-open-questions.webp)
*Three independent decisions remain open. The arrow expands a clipped preview; Read and answer opens that question's response controls.*

After submission, the recorded answer remains in the conversation. A question
from a delegated agent identifies its owner; the response goes back to that agent.

Permission requests, plan review and choosing between generated drafts have
their own controls. A normal confirmation question does not replace approval
for a protected action.

- [Follow work in progress](docs/following-work.md): Tell a running operation from a checklist item or a completed result.
- [Permissions](docs/permissions.md): Review an action before allowing it to run.
- [Compare generated drafts](docs/best-of-n.md): Inspect alternatives and choose the draft to keep.
