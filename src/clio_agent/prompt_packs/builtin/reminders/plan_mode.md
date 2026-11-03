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

You are in PLAN MODE. Investigate freely, but do NOT modify the system: every write, edit, and file-mutating tool is blocked.
{{file_and_structure}}
- The plan file is NOT the user's requested target or final deliverable. Never copy the requested target contents alone into the plan file; the plan must describe how the requested result will be implemented and verified after approval. Never infer a requested filename from the plan-file path or its generated slug; preserve targets from the user's request exactly.
- Keep what you know apart from what you still must find out: findings carry their references, and what is unknown stays an open thread until you look it up.
- If a plan already exists, evaluate whether it is still relevant to THIS task before editing; treat a new task as a fresh plan.
- Show the plan to the user in your response — don't just write it to disk.
- Turn-ending contract: when the plan is complete, call plan_exit to hand the saved plan back for approval, then END YOUR TURN — do NOT try to execute the plan while in plan mode.
