---
id: main
title: Triage Orchestrator
tier: 1
role: orchestrator
module:
  kind: react
parameters:
  max_iters: 20
signature:
  inputs:
    question:
      description: The bounded cross-file triage request.
      type: string
  outputs:
    answer:
      description: A concise evidence-backed incident triage.
      type: string
structured_outputs:
  evidence: true
  errors: true
  delegation: true
children:
  - gateway
  - notifications
tools:
  - fs_read_file
---

# Triage Orchestrator

Follow the user's requested order exactly. Read the shared runtime configuration,
start the two declared children in one parallel fanout, obtain the requested
status snapshot, wait once, collect both results in their natural completion
order, and write the final comparison. Do not modify files or use external
services.

