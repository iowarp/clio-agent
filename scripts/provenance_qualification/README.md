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

### Native-wheel host mismatch

If the host cannot install the locked `iowarp-core` wheel (for example, an
older glibc), do not reuse an older venv or select LocalFS. Build the exact
qualification image from the repository root:

```bash
docker build -f scripts/provenance_qualification/Dockerfile \
  -t clio-provenance-qualification:exact .
```

Run it with host networking and bind only the isolated qualification runtime,
Flowcept settings, and provider authentication required for the run. The pinned
`iowarp-core` runtime uses `io_uring`; Docker's default seccomp profile denies
`io_uring_setup`, so the qualification container must include the explicit
`--security-opt seccomp=unconfined` option:

```bash
docker run --rm --network host \
  --security-opt seccomp=unconfined \
  --ulimit core=0 \
  --env-file deployment.env \
  --mount type=bind,src="$CLIO_PQ_RUNTIME",dst=/qualification \
  clio-provenance-qualification:exact
```

This exception is limited to the disposable qualification container; it is not
a general host security change. Core dumps are disabled for the disposable run
so a native shutdown cannot silently consume the qualification disk. The image
contains the frozen CLIO/Flowcept environment, CTE, and a separate Python 3.9
CMF worker. Use a new, empty runtime mount for every acceptance run, then remove
it after its evidence is captured.
The startup preflight probes `io_uring` before starting CTE and reports this
missing container option directly instead of surfacing iowarp-core's misleading
`Failed to open file` message.

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
