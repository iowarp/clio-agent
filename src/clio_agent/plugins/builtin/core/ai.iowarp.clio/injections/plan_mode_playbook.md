---
id: clio.reminder.plan_mode_playbook
title: Plan mode, operator playbook
description: Presents an operator playbook's ordered steps as the required plan skeleton, in place of the structure bullet. label is the playbook's quoted name after a space, or empty; steps are its numbered lines with each step's guidance and allowed tools.
x_kind: reminder
x_active: true
x_required: carries the operator's plan skeleton and each step's allowed tools, which the permission gate enforces while that step is active
x_trigger: plan_mode, in the full contract after the plan-file bullet, when a skill has recorded an operator playbook
x_variables: label, steps
---
Follow the operator playbook{{label}} as the REQUIRED plan structure — author one plan section per numbered step below, in order; do not substitute your own top-level structure:
{{steps}}
