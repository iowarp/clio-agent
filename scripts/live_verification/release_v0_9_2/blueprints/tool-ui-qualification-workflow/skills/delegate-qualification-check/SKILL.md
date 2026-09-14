---
name: delegate-qualification-check
title: Delegate a Qualification Check
description: Check a bounded supplied arithmetic claim in a fresh child turn.
effect: {kind: spawn_subagent_with_skill, agent: verification}
---

You are running in the delegated skill child. Check only the supplied Alpha and
Beta values. Return one sentence stating whether Alpha plus Beta equals the
supplied expected total. Do not call tools or create files.

