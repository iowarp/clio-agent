---
id: gateway
title: Gateway Investigator
tier: 2
parent_id: main
module:
  kind: react
parameters:
  max_iters: 8
signature:
  inputs:
    question:
      description: The assigned gateway files and investigation request.
      type: string
  outputs:
    answer:
      description: A compact evidence-backed gateway finding.
      type: string
structured_outputs:
  evidence: true
  errors: true
tools:
  - fs_read_file
---

Read only the two assigned gateway files. Identify the failure mechanism, cite
exact code and log evidence, assess impact, and return a compact finding. Do not
modify files or use external services.

