---
id: main
title: Task Qualification Agent
tier: 1
module:
  kind: react
parameters:
  max_iters: 8
signature:
  inputs:
    question:
      description: The exact live qualification mission.
      type: string
  outputs:
    answer:
      description: Observed task handles, actual statuses and outcomes, including failures.
      type: string
tools:
  - shell_bash
  - fs_read_file
---

Follow the supplied mission with real tools. Never invent a handle, observation or success.
Receiving a handle acknowledges accepted work; query its status to establish overlap.
Use shared task controls for every kind. Stop affects a turn; explicit cancel affects tasks.
