---
id: clio.reminder.circuit_breaker_warning
title: Warn that a tool keeps failing
description: Warns the agent, at the failure limit, that further calls to the tool will be blocked.
x_kind: reminder
x_active: true
x_trigger: source circuit_breaker: the call that brings a tool to tools.circuit_breaker.failure_limit transient failures in a row; added to its error or result, after the fixed marker [clio: circuit_breaker]
x_variables: name, count
---
{{name}} has failed {{count}} times in a row. Consider an alternative route: further calls to it will be blocked to prevent looping.
