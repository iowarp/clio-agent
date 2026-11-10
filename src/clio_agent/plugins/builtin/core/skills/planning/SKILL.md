---
name: planning
title: Plan for the person's approval before acting
description: Use when the person wants to approve a plan before anything changes, or an unresolved decision needs their approval (Plan Mode with plan_enter, plan_update and plan_exit). A plan you write and carry out yourself needs only plan_update.
---

# Planning

Start with `plan_enter`, which puts the session in **Plan Mode**: writes, edits and
file-mutating tools are blocked (declared read-only tools stay open), and the plan is
the one thing you change, through `plan_update`. You are here to produce a plan the
user approves, not to execute it. Work the four phases below in order, then end your
turn.
Routine, authorized creation of reports, slides or other deliverables does not need
this workflow.
Tools that would change files are refused. The shell stays available only where the
OS write fence confines it to reading; otherwise use the session's file-reading and
inspection tools. If those cannot inspect a needed part of the work, state that
specific capability gap; do not treat it as missing or claim that uninspected code
or data has been verified.

## Phase 1 — Ground (explore before you ask)
Do not ask the user what the repository can answer. Investigate first: read the
relevant files and use the available read-only inspection tools. When the work spans
several areas that each need judgement to read and this agent can delegate, `explore`
children (one per area, in one `spawn_agents_parallel` call with `wait=true`) read
them in parallel and report back; a survey a script can do mechanically, such as
listing every module's docstring, is faster as one script, and a small task in one
place needs neither.
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
proposal the user weighs in on. For those, the strategy and open questions come
before the detailed plan, in an earlier turn, because the user's answer can change
what the detail should be.

## Phase 3 — Draft (write incrementally with plan_update)
Write the plan with `plan_update(content=...)` and revise it as you learn
(`old` and `new` replace one passage). Fit the plan to the task. A small change needs
only Changes and Verification. Larger or open-ended work:
- **Objective**: the question, what a complete answer contains, what counts as done.
- **Approach**: how you will get there.
- **Threads**: one per line, each marked at the start `[open]`, `[active]`, `[closed]`
  or `[dropped]` (`- [open] name: what it asks`), so CLIO can count what is left; a
  one-line reason when it ends and the handles of any children working on it.
- **Findings**: each a claim with its references (`call:` refs, paths and lines,
  handles); a corrected finding is marked superseded rather than deleted.
- Checking your claims is the last thread.

Keep what you know apart from what you still must find out: findings carry their
references, and an unknown stays an open thread until you look it up. If a plan
already exists, judge whether it still fits this task before editing; treat a
genuinely new task as a fresh plan.

A Mermaid diagram can clarify dependencies, a protocol or a proposed workflow
in the plan. The active Mermaid component entry documents its supported
notations and syntax; `create-diagram` covers selection and review. Use them
when useful, within Plan Mode's
existing tool boundaries. A list or prose can be clearer; diagrams are optional.

## Phase 4 — Show & exit (the turn-ending contract)
Show the plan to the user in your response, since a plan left only on disk is easy
to miss. Then end your turn by handing it back: call `plan_exit` (with a short summary
and an optional recommended posture) when the plan is complete, or the ask-user
question tool if you need a decision before finishing. Executing waits for the user's
approval, after which you resume with authorization.
