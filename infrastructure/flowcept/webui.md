---
name: flowcept-webui
component: flowcept
kind: webui
services: [flowcept-webservice]
ports:
  webservice: 8008 (loopback, homelab)
status: deployed-loopback-homelab
---

# Flowcept — native web UI

Flowcept ships its own webservice + UI as a **separate** compose stack
(upstream `deployment/compose-service.yml`). It reads the same MongoDB the
capture stack persists into and, under the `full-online` profile, offers
live and historical workflow queries, workflow cards, and agent chat.

## Deployment state

Running on the homelab as `flowcept_webservice`, loopback `:8008` (verified
2026-08-25, up alongside the Redis/Mongo capture stack). To view from a
workstation:

```
ssh -L 8008:127.0.0.1:8008 homelab
# then open http://localhost:8008
```

## Role

Inspection/ops surface only. The CLIO product UI (gact-tui) reads CLIO's
provider-neutral REST routes, never Flowcept's UI or database — this UI is
for verifying what Flowcept recorded, independent of CLIO.
