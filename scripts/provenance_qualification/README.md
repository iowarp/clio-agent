# Provenance live-qualification recipe

Reproducible harness for the Flowcept/CMF provenance qualifications recorded
in `docs/design/provenance-adapters-and-artifact-storage-2026-08.md`:

- §14 — the original 2026-08-22 write-plane qualification (Flowcept
  full-online + CMF/PostgreSQL, custody + producer-independence).
- §14.x — the 2026-08-26 edge requalification (`b=transform(a)`: registry
  artifacts + INPUT/OUTPUT MLMD events, published to the CMF server).

Nothing here is host-specific: every deployment parameter comes from an env
file (`deployment.env` pattern). `homelab.env.sample` records the exact
values of the qualified deployment.

## Files

- `serve-qualification.sh` — starts a CLIO serve with the CMF + Flowcept
  provider configuration, entirely from `$CLIO_PQ_*` variables.
- `edges-drive.sh` — the `b=transform(a)` driver: workspace → allow-all
  policies → session → turn 1 writes `a.csv` → turn 2 reads it and writes
  `b.csv` → dumps every MLMD artifact/execution/INPUT/OUTPUT event via the
  isolated CMF worker runtime.
- `homelab.env.sample` — the qualified deployment's parameter set.

## Run

```bash
cp homelab.env.sample deployment.env   # edit for your deployment
set -a; . ./deployment.env; set +a
./serve-qualification.sh &             # or under setsid/nohup
./edges-drive.sh
```

Acceptance (edge requalification): the final MLMD dump shows the written
files as `clio://artifact/...` nodes (never `clio://external/...` fallbacks)
and three events — `OUTPUT -> a.csv` (turn 1), `INPUT -> a.csv` (turn 2
read), `OUTPUT -> b.csv` (turn 2 write). Failures on the CMF path surface as
`provenance provider cmf degraded on emit ...` WARNINGs in the serve log
(first failure per worker is loud by design — never rely on silence).

## Prerequisites

- The backing services (per `infrastructure/`): CMF server + PostgreSQL for
  the publish step; Flowcept Redis + MongoDB when the flowcept provider is
  selected.
- An isolated CMF-compatible Python (3.9, `cmflib==0.1.0`,
  `ml-metadata==1.15.0`) at `$CLIO_PQ_CMF_PYTHON` — CLIO's own interpreter
  never imports cmflib.
- A lock-synchronized CLIO interpreter containing the pinned `clio-schemas`,
  LiteLLM, `psutil`, and `iowarp-core` runtime. `serve-qualification.sh`
  verifies these before binding a port and rejects LocalFS; an overlay or a
  host with an incompatible `iowarp-core` wheel is not qualification evidence.
- A fresh `$CLIO_PQ_CMF_METADATA_PATH` per qualification run: MLMD types are
  first-writer-wins per name, so re-using a store predating the consistent
  type-schema fix (#1247) rejects typed mints.
