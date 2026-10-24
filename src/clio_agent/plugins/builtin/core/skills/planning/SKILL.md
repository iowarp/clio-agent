---
name: planning
title: Plan the work before implementation
description: Produce an approval plan when the user asks to plan before execution or an unresolved implementation decision needs approval. Loading this skill enters Plan Mode and stops ordinary writes. Routine authorized creation of reports, slides or other deliverables does not require this separate planning workflow.
effect: {kind: "enter_mode", mode: "plan"}
---

# Planning

Invoking this skill puts the session in **Plan Mode**: every write, edit, and
file-mutating tool is blocked (declared read-only tools stay open), and the ONLY
writable path is your plan file. You are here to produce a plan the user approves —
not to execute it. Work the four phases below in order, then end your turn.
Shell tools are blocked even for inspection commands. Use the session's declared
file-reading and inspection tools. If those cannot inspect a needed part of the
repository, state that specific capability gap; do not treat it as a missing
repository or claim that uninspected code has been verified.

## Phase 1 — Ground (explore before you ask)
Do not ask the user what the repository can answer. Investigate first: read the
relevant files and use the available read-only inspection tools. For independent
investigations, read-only explore subagents can help when this agent has a
delegation capability; a small task does not need them.
Discover the checkout's actual directories with `fs_list_directory` before guessing
paths. Use `fs_search_text` for definitions, callers and regression tests, then read
the relevant files. Narrow searches when a result is truncated; an incomplete
search cannot establish absence. These tools inspect files without running shell.
Establish the current behavior before proposing a change: the request may already
be implemented. Resolve source questions during planning rather than deferring
them to implementation. Report an existing capability or a demonstrated gap;
do not manufacture work to match the request's wording.
Search tests by observable behavior, messages and test names as well as source
symbols. A test can exercise a feature without naming its internal exception or
helper. Check matching test bodies before declaring coverage missing.
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

A Mermaid diagram can clarify dependencies, a protocol or a proposed workflow
in the plan. The active Mermaid component entry documents its supported
notations and syntax; `create-diagram` covers selection and review. Use them
when useful, within Plan Mode's
existing tool boundaries. A list or prose can be clearer; diagrams are optional.

## Phase 4 — Show & exit (the turn-ending contract)
Show the plan to the user in your response — do not just leave it on disk. Then
**END YOUR TURN** by handing the plan back: call `plan_exit` (with a short summary and an
optional recommended posture) when the plan is complete, or the ask-user question
tool if you need a decision before finishing. Do NOT try to execute the plan while
in plan mode — you resume with authorization only after the user approves.
