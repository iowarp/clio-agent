---
id: main
title: Task Qualification Coordinator
tier: 1
module:
  kind: react
parameters:
  max_iters: 8
signature:
  inputs:
    question:
      description: The exact qualification mission to coordinate.
      type: string
  outputs:
    answer:
      description: Actual observed handles, status and outcomes.
      type: string
children:
  - worker
tools:
  - fs_read_file
---

Spawn only the explicitly requested worker assignment. Use shared task controls
yourself for status and waits; do not delegate those observations to the worker.
Never invent a handle or outcome.
