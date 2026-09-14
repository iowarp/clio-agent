---
name: maintain-scientific-dossier
title: Maintain the Scientific Dossier
description: Keep research identity, evidence, assumptions, decisions, resources, artifacts, and verification claims traceable across the conversation.
---

Use this skill whenever the research state materially changes. Distinguish
scientist-supplied fact, external evidence, expert inference, proposed default,
accepted decision, unresolved question, and unverified artifact claim.

Record each consequential decision with a stable id and sequence, question,
answer and units, alternatives, rationale and evidence, owner, affected
assumptions/artifacts, and status: proposed, accepted, superseded, or reopened.
Never overwrite history. A correction names what it supersedes and what must be
revisited.

Keep artifact status explicit: generated, statically reviewed,
scientist-approved, solver-executed, or validated. When the dossier or another
deliverable becomes useful outside chat, call the runtime-provided
`create_artifact` with complete content, an appropriate workspace-relative name,
and exact inputs in `used`. Preserve the returned artifact identity and version;
do not claim creation after a rejected call.
