---
id: verification
title: Supplied Fact Verification
tier: 2
parent_id: main
module:
  kind: chain_of_thought
signature:
  inputs:
    question:
      description: The supplied facts to verify.
      type: string
  outputs:
    answer:
      description: Typed verification state.
      type: string
structured_outputs:
  workflow_state: true
---

Return exactly one compact JSON object recording that Alpha is 4, Beta is 7,
their sum is 11, and workflow_state.verification.status is complete. Use no
tools.

