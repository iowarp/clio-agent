---
name: evidence-fanout
title: Coordinate Scientific Evidence Fan-out
description: Decompose one evidence question into independent source lanes, follow consequential leads, obtain independent criticism, and return a complete source audit.
---

Use this skill only in the scientific evidence coordinator. It defines a dynamic
evidence investigation, not a fixed lane count or generic literature review.

Before creating evidence children, read
[fanout-lifecycle.md](references/fanout-lifecycle.md) for lane boundaries,
parallel spawn/wait behavior, evidence-driven follow-ups, critic iteration, and
blocked-task handling.

Before accepting claims or preparing the critic packet, read
[source-integrity.md](references/source-integrity.md) for fetched-source rules,
claim mapping, citation placement, source disposition, and incomplete-evidence
reporting.

The coordinator does not search the web. Assign web discovery and reading to
`evidence_leaf`; assign independent verification to `evidence_critic`. Use
`ask_user` directly only when the evidence boundary depends on a scientist-owned
definition or inclusion choice. Preserve every child task id and synthesize only
returned evidence.
