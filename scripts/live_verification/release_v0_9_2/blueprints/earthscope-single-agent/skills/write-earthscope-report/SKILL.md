---
name: write-earthscope-report
title: Write an EarthScope Report
description: Create a compact Markdown report artifact whose claims and provenance are copied from the session's observed scientific evidence.
---

Use this skill when the user asks for a durable report or export. Write concise
Markdown covering the question, resolved region, station selection, source
resource, staged data, analysis/plot scope, limitations, and artifact references.
Every numeric value, identifier, URL, and path must already exist in retained
typed state or tool evidence.

Call `create_artifact` with a workspace-relative `.md` name, `kind="report"`, the
complete Markdown as `content`, and `used` containing the exact staged CSV and
plot paths that the report derives from. Preserve the returned artifact id and
version. Do not claim the report exists if creation was rejected, and do not
silently overwrite an unrelated existing file.
