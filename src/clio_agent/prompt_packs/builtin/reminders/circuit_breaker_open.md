---
id: clio.reminder.circuit_breaker_open
title: Refuse a call to a tool that keeps failing
description: Tells the agent a call was not run because the tool failed too many times in a row.
x_kind: reminder
x_active: true
x_trigger: source circuit_breaker: a call to a tool that has failed tools.circuit_breaker.failure_limit times in a row; it is the call's error, after the fixed marker [clio: circuit_breaker]
x_variables: name, count, last_error
x_required: the call was not run; this is its only result, so without it the agent would not know why
---
{{name}} was not run: it failed {{count}} times in a row (last error: {{last_error}}). Take an alternative route, or report what blocks you -- repeating this call would loop.
