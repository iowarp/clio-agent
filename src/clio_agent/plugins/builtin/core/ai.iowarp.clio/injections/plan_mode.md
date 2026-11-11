---
id: clio.reminder.plan_mode
title: Plan mode contract
description: The full plan-mode contract. Its first bullets come from the plan_mode_* pieces (where the plan is saved, then the plan's structure); the rest is fixed here.
x_kind: reminder
x_active: true
x_required: in plan mode every write is blocked and the turn must end with plan_exit; a person can put the session in plan mode from the app, and this is the only place the agent learns it is in plan mode and how to leave it
x_trigger: in plan mode, on the first plan-mode turn, after a compaction, every full_interval turns of the plan variant (10, or 20 for plan_small) and as the result of plan_enter
x_variables: file_and_structure
---
## Plan Mode active — read-only except the plan file

You are in plan mode. Investigate freely; nothing else changes until the plan is approved, so every write, edit and file-mutating tool is blocked except plan_update.
{{file_and_structure}}
- The plan file holds your plan, not the deliverable the person asked for: it says how the requested result will be made and checked after approval. The deliverable's names and places come from the person's request; the plan file's path is CLIO's own.
- Keep what you know apart from what you still must find out: findings carry their references, and what is unknown stays an open thread until you look it up.
- If a plan already exists, judge whether it still fits this task before editing it; a new task gets a fresh plan.
- Show the plan to the person in your reply as well; they read your reply, not the file.
- Ending the turn: when the plan is complete, call plan_exit to hand the saved plan back for approval and end your turn there. Nothing is carried out in plan mode; the work starts after approval.
