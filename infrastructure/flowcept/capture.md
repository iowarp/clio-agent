---
name: flowcept-capture
component: flowcept
kind: capture
services: [redis, mongodb]
ports:
  redis: 6379 (loopback)
  mongodb: 27017 (loopback)
status: qualified-live-2026-08-22
---

# Flowcept — capture infrastructure

Backend for CLIO's **agentic provenance stream** (turns, tool calls,
workflow/task events). CLIO writes only through the Flowcept library's
supported ingestion path — it never talks to Redis or MongoDB directly, so
swapping Flowcept profiles requires no CLIO change.

## Required services (`full-online` profile)

- **Redis** — Flowcept's MQ/KV transport (`redis:7-alpine`).
- **MongoDB** — persisted workflow/task collections (`mongo:8.0`).

Alternative profiles need neither (offline JSONL / LMDB), but only
`full-online` supports live + historical queries, the Flowcept UI, and the
Spotter provenance MCP's Flowcept lane.

## Reference deployment (homelab)

`/home/jcernuda/compose/flowcept/docker-compose.yml` (Dockge-managed).
MongoDB data in the named volume `flowcept_flowcept_mongo_data`. Both
services bind **loopback only** — the stack runs without Redis/MongoDB
authentication, so it must never bind the LAN as-is. Upstream equivalents:
`deployment/compose-mongo.yml` / `make services-mongo`.

## CLIO side

- Install the `flowcept` extra (`uv sync --extra flowcept`).
- Select with `CLIO_PROVENANCE_PROVIDERS=flowcept` (default is `jsonl`).
- Qualified live: 273 accepted events, zero dispatcher failures, native
  persistence genuinely off while selected.
