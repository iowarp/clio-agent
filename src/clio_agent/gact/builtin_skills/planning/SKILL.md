---
name: planning
description: Enter Plan Mode and produce an approved implementation plan before touching the system — ground in evidence, consult the user, draft incrementally to the plan file, then hand it back for approval.
effect: {kind: "enter_mode", mode: "plan"}
---

# Planning

Invoking this skill puts the session in **Plan Mode**: every write, edit, and
file-mutating tool is blocked (reads and read-only shell stay open), and the ONLY
writable path is your plan file. You are here to produce a plan the user approves —
not to execute it. Work the four phases below in order, then end your turn.

## Phase 1 — Ground (explore before you ask)
Do not ask the user what the repository can answer. Investigate first: read the
relevant files, run read-only commands, and — for anything non-trivial — launch
read-only explore subagents in parallel to map the code, tests, and data paths.
Turn unknowns into evidence before you propose anything. "Do not ask what the repo
can answer" is the rule; a question to the user is for genuine intent/tradeoff
decisions, not for facts you can look up.

## Phase 2 — Consult (depth proportional to complexity)
Match consultation to the task's size. A small, unambiguous change needs little or
no back-and-forth; a large or architectural one needs an explicit strategy
proposal the user weighs in on. Propose the strategy and open questions FIRST, and
do NOT draft the full plan in the same turn as the strategy proposal — let the user
steer before you commit detail to the plan file.

## Phase 3 — Draft (write incrementally to the plan file)
Write the plan to your plan file and edit it incrementally as you learn — it is the
sole writable path. Fit the structure to the task:
- Simple change → Changes + Verification.
- Standard task → Objective, Key Files & Context, Implementation Steps, Verification.
- Complex / architectural → Background, Scope, Proposed Solution, Alternatives, a
  phased Plan, Verification, Migration/Rollback.

Keep an **epistemic ledger** in the plan under the headers `Given` / `Learned` /
`To look up` / `To derive`, so what you know is separated from what you still must
find out. If a plan file already exists, judge whether it still fits THIS task
before editing; treat a genuinely new task as a fresh plan.

## Phase 4 — Show & exit (the turn-ending contract)
Show the plan to the user in your response — do not just leave it on disk. Then
**END YOUR TURN** by handing the plan back: call `plan_exit` (with a short summary and an
optional recommended posture) when the plan is complete, or the ask-user question
tool if you need a decision before finishing. Do NOT try to execute the plan while
in plan mode — you resume with authorization only after the user approves.
