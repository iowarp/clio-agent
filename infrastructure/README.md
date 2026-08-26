# Infrastructure catalog

Light, per-component descriptions of the external infrastructure CLIO
integrations depend on: what has to run for **capture** (the write path)
and what the component's **native web UI** is (the inspection path).

Each component gets one folder with two files:

- `capture.md` — the services required for CLIO to capture into this
  backend, the CLIO config that selects it, and the reference deployment.
- `webui.md` — the component's own native web UI (never CLIO's): what it
  shows, where it runs, and how to reach it.

Files carry a small YAML front-matter block (`name`, `kind`, `services`,
`ports`, `status`) so a future consumer can enumerate them mechanically.

## Intent

These are the seeds for the clio-agent UI **infrastructure view**: each
entry is meant to become deployable/manageable from the UI rather than
hand-run compose stacks. Planned entries beyond the current two:
**clio web search** and **clio relay**.

## Current entries

| Component | Role |
| --- | --- |
| `flowcept/` | Agentic provenance stream backend (Flowcept full-online) |
| `cmf/` | Artifact provenance + custody backend (HPE CMF) |

Deployment specifics below are as qualified live on 2026-08-22 (see
`docs/design/provenance-adapters-and-artifact-storage-2026-08.md` on the
`feat/flowcept-provenance` branch until it lands).
