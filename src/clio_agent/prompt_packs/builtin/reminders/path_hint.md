---
id: clio.reminder.path_hint
title: Suggest existing files for a missing path
description: After a failed call, names same-named files under the allowed roots for each path argument that does not exist; the agent chooses.
x_kind: reminder
x_active: true
x_trigger: source path_hint: a tool call failed and one of its path arguments does not exist while files of that name do; one line per argument, after the fixed marker [clio: path_hint]
x_variables: argument, value, options
---
argument '{{argument}}': '{{value}}' does not exist. Did you mean {{options}}?
