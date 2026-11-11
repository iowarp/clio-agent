---
id: clio.reminder.variant_drafting
title: Say that alternatives are being drafted
description: Shows how the agent's answer is being drafted: how many tries, who picks, and the rubric. what, capped and who are the variant_drafting_* pieces.
x_kind: reminder
x_active: true
x_trigger: source variant_drafting: when draft_alternatives or a best-of-N or refine agent starts drafting
x_variables: what, capped, who, rubric
---
Drafting {{what}}{{capped}}; {{who}}. Rubric: {{rubric}}
