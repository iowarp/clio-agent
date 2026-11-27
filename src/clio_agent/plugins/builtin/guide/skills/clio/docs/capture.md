---
title: "Turn your work into a plugin"
description: "Use /capture to turn what you did in a conversation into a plugin you can reuse, and /optimize to improve a plugin from the conversations that used it."
source: https://clio.iowarp.ai/docs/capture/
---

# Turn your work into a plugin

Use /capture to turn what you did in a conversation into a plugin you can reuse, and /optimize to improve a plugin from the conversations that used it.

When you have worked out how to do something in CLIO, such as reading your instrument's
files, checking a run, or making the plots your group expects, `/capture` turns that
conversation into a plugin. Next time, CLIO already knows how. You do not write any
files and you do not need to know what the plugin is made of.

## Capture a conversation

1. At the end of the conversation (or partway through), type `/capture`, optionally
   followed by what you want to keep: `/capture the unit check and the plots`. To combine
   work spread over several conversations, refer to them with `@`.

2. CLIO reads the whole conversation, including the helpers it started, and shows the
   procedure it saw as steps, with what looks general and what looks like a one-off.

3. For each concrete value (a file, a dataset, a threshold, a unit), it asks whether it
   stays the same in your lab or changes each time. Values that change become inputs.

4. It proposes the parts, in plain words: for example a procedure to follow, a rule it
   must never break, a calculation to do the same way every time, a command to start it.
   It also says what it chose not to make. Nothing is written until you approve.

5. It writes the plugin, checks it, and has a separate reviewer read it without the
   conversation, the way someone new would.

6. It offers a trial: one or two tasks from the conversation, restated as a newcomer
   would ask them, answered once with the plugin and once without, side by side. You say
   which is right. Say "skip it" to skip the trial.

7. The plugin is saved in your **Private local marketplace** and added to the
   conversation. CLIO can also offer to save your agent with it, so new conversations
   start with it.

You can also say it in your own words: "make this reusable", "I want to do this again
next week", "teach CLIO this".

> **Note: Trusting what CLIO wrote**
>
> A plugin with only text (procedures, rules, commands) is ready at once. A plugin with
> code (a calculation, a check that refuses actions) is shown to you once, with what each
> part does and what its code can reach, before it first runs. When a new version changes
> the code, you are asked again, and the changed files are named. A plugin from anyone else
> is trusted only in **Settings**, never from a conversation.

## Improve a plugin with /optimize

`/optimize <plugin>` reads the conversations where the plugin was used and makes its next
version: the procedure that was not followed when it should have been, a correction you
had to repeat, a rule that refused too much or too little, a step you kept doing by hand.
It shows which conversations it will read before it reads them. The trial then compares
the current version with the new one on tasks where the current one fell short.

CLIO changes only plugins it wrote for you. For a plugin from a marketplace, including
CLIO's own, it writes your variation under a new name and leaves the original as it is.

## Look across all your work

`/optimize` with no plugin named looks at your conversations since the last time you ran
it (the last 30 days the first time):

1. It shows how many conversations it will read, and from when.
2. It reads them in parallel, one helper each. You can run these helpers on a cheaper
   model: in **Settings > Models**, under **Helper models**, choose a model for
   **Optimize**.
3. It reports, per plugin, what went wrong and how often, with links to the
   conversations; then the procedures no plugin covers; then the problems no plugin can
   fix, such as a provider error.
4. You pick what to act on. Each picked plugin gets a new version and each gap becomes a
   new plugin, one at a time, through the same questions as `/capture`.

Something seen in only one conversation is listed but not proposed, unless it was
serious. What you decline is remembered and not proposed again unless it happens in new
conversations, and the next run starts where this one ended.
