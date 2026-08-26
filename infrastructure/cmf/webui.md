---
name: cmf-webui
component: cmf
kind: webui
services: [cmf-ui]
ports:
  cmf-ui: 8381 (loopback, homelab)
status: deployed-loopback-homelab
---

# CMF — native web UI

CMF ships a web GUI with its server stack. It renders **artifact,
execution, and artifact-execution trees** over the MLMD store — a plain
inspection browser, not a dashboard. An optional Neo4j graph layer exists
upstream (`graph=True`) but is not deployed here.

## Deployment state

Running on the homelab at loopback `:8381` (next to cmf-server `:8380`),
as part of the lean qualification profile. To view it from a workstation:

```
ssh -L 8381:127.0.0.1:8381 homelab
# then open http://localhost:8381
```

## Role

Inspection/ops surface only — useful for verifying what CMF recorded
(e.g. the current four-nodes-zero-edges lineage state). The CLIO product
UI queries CLIO's provider-neutral artifact-graph REST; CMF's web UI is
never part of that contract.
