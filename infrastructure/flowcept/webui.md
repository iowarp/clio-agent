---
name: flowcept-webui
component: flowcept
kind: webui
services: [flowcept-webservice]
ports:
  webservice: not-deployed (see below)
status: not-deployed-on-homelab
---

# Flowcept — native web UI

Flowcept ships its own webservice + UI as a **separate** compose stack
(upstream `deployment/compose-service.yml`). It reads the same MongoDB the
capture stack persists into and, under the `full-online` profile, offers
live and historical workflow queries, workflow cards, and agent chat.

## Deployment state

**Not part of the homelab qualification stack.** Only Redis + MongoDB were
deployed there; the webservice/UI can be added later from
`compose-service.yml` pointed at the same MongoDB. Port assignment happens
at that deployment (keep it loopback like the rest of the stack).

## Role

Inspection/ops surface only. The CLIO product UI (gact-tui) reads CLIO's
provider-neutral REST routes, never Flowcept's UI or database — this UI is
for verifying what Flowcept recorded, independent of CLIO.
