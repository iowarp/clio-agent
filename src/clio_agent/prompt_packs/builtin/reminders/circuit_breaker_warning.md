---
id: clio.reminder.circuit_breaker_warning
title: Say that a tool keeps failing
description: A fact for the agent when one tool has failed several times in a row; nothing is blocked, the agent decides.
x_kind: reminder
x_active: true
x_trigger: source circuit_breaker: the call that brings a tool to `after` failures in a row (a success resets the count); added to its error or result, after the fixed marker [clio: circuit_breaker]
x_variables: name, count
x_param_after: 3
---
{{name}} has failed {{count}} times in a row. Unless something has changed, calling it the same way again is likely to fail again.
