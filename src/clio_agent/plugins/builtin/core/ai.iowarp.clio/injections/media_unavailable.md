---
id: clio.reminder.media_unavailable
title: Say a document was not read (Codex)
description: Stands in for a document the connected Codex backend refused, and is shown in the transcript when that happens.
x_kind: reminder
x_active: true
x_trigger: when the Codex backend refuses native document input because its file preparation is unsupported; replaces each refused document in later requests
x_required: it stands in for a document the backend refused; without it the agent would not know the document's contents were never read
---
CLIO: The connected Codex backend rejected native document input because its file preparation is unsupported. The document's contents were NOT read. Do not repeat view_pdf for this document. Use its workspace path from the question or tool call with prepare_document(action='inspect') for bounded extracted text. If visual evidence is needed, render the relevant pages and inspect them with view_image when available. Continue other work and state any remaining evidence limitation.
