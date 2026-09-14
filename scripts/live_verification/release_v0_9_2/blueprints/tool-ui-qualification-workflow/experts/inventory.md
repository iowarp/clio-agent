---
id: inventory
title: Supplied Fact Inventory
tier: 2
parent_id: main
module:
  kind: chain_of_thought
signature:
  inputs:
    question:
      description: The supplied facts to record.
      type: string
  outputs:
    answer:
      description: Typed inventory state.
      type: string
structured_outputs:
  workflow_state: true
---

Return exactly one compact JSON object recording that Alpha is 4, Beta is 7,
and workflow_state.inventory.status is complete. Use no tools.

