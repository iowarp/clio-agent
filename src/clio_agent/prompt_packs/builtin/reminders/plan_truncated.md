---
id: clio.reminder.plan_truncated
title: The plan continues
description: Ends a plan cut at the injection limit (plan.injection_chars) with where to read the rest. Part of plan and plan_person_edit.
x_kind: reminder
x_active: true
x_trigger: plan, after the plan text, when the plan is longer than plan.injection_chars
x_variables: path
---
[The plan continues; read it whole at {{path}} with fs_read_file.]
