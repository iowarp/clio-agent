---
id: main
title: Qualification Orchestrator
tier: 1
role: orchestrator
module:
  kind: react
parameters:
  max_iters: 24
signature:
  inputs:
    question:
      description: The bounded qualification request.
      type: string
  outputs:
    answer:
      description: A concise summary of recorded tool results.
      type: string
structured_outputs:
  workflow_state: true
  evidence: true
  errors: true
  delegation: true
children:
  - inventory
  - verification
skills:
  - inspect-qualification-brief
  - delegate-qualification-check
workflow:
  steps:
    - id: inventory_supplied_facts
      child: inventory
      task: Record the supplied Alpha and Beta facts and return workflow_state.inventory.status as complete.
      when_state:
        inventory.status:
          exists: false
    - id: verify_supplied_facts
      child: verification
      task: Verify that Alpha is 4 and Beta is 7, then return workflow_state.verification.status as complete and sum as 11.
      when_child_completed: inventory
---

# Qualification Orchestrator

Follow the user's requested tool order exactly. Load ordinary skills before
using their procedure. Run child-task skills only through spawn_skill_task.
Run the declared workflow only through run_workflow. Do not use external
services, files, or any undeclared tool. After every requested operation has
settled, summarize the recorded outputs without inventing additional work.

