---
name: cmf-capture
component: cmf
kind: capture
services: [cmf-server, postgresql, cmf-worker]
ports:
  cmf-server: 8380 (loopback)
status: qualified-live-2026-08-22
---

# CMF — capture infrastructure

Backend for CLIO's **artifact provenance + custody** axis (HPE Common
Metadata Framework). CLIO records artifact identity and custody through an
isolated worker that writes CMF's MLMD store; custody is DVC-compatible
(content-addressed `files/md5/...` objects).

## Required services

- **cmf-server** — CMF REST API (loopback `:8380` in the reference deploy).
- **PostgreSQL** — cmf-server's backing store.
- **CMF worker** — an isolated **Python 3.9** virtualenv running
  `cmflib==0.1.0` + `ml-metadata==1.15.0`. It imports no CLIO code; CLIO's
  3.12 process never imports cmflib. The worker's venv/launcher must be
  preserved across deploys.

Deliberately NOT deployed (upstream full compose extras): TensorBoard,
Nginx, MinIO, the upstream CMF MCP. The stock stack has no resource
limits, permissive CORS, and no application auth — keep everything on
loopback; production needs auth, backups, and limits first.

## CLIO side

- Select with `CLIO_ARTIFACT_PROVENANCE_PROVIDER=cmf` (default `native`).
- Qualified live: artifact custody proven end-to-end (CLIO SHA-256 ↔ CMF
  native MD5 ↔ on-disk CAS object), 5 accepted artifact events, native CAS
  genuinely off while selected. Known gap: lineage edges (input/output
  transform relationships) were not yet demonstrated — identity and
  custody were.
