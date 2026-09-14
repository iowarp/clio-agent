---
name: coordinate-scientific-work
title: Coordinate Scientific Work
description: Decide whether to answer, clarify, load a method, or consult specialists, then preserve task identity and synthesize only settled results. Greetings and ordinary conceptual questions are answered directly and do not need this skill.
---

Use this skill when the current scientific request may benefit from clarification,
a specialist consultation, or independent parallel work. It is not a required
prelude to greetings or ordinary questions.

Before delegating, read
[decision-criteria.md](references/decision-criteria.md). It distinguishes direct
answers, `ask_user`, focused skills, one consultation, and independent fan-out.

When a consultation is selected, read
[task-lifecycle.md](references/task-lifecycle.md) before spawning. It defines task
briefs, spawn and wait calls, accepted/queued work, durable task ids, child-owned
questions, same-task resumption, failure handling, and result synthesis.

Apply only the branch relevant to the request. A simple answer does not need an
expert, and a single dependent question does not become parallel work. Keep the
scientist-facing response about the science rather than the coordination
topology. Never claim that a consultation completed until its returned task state
and result say so.
