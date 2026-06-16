# Two CLIOs talking clio-to-clio over a shared clio-core runtime

A real multi-process demonstration: two **separate** clio processes share one
clio-core runtime and hand a delegation between them. A *parent* clio delegates a
data task; a *worker* clio runs a real expert (ALCF) and the answer flows back —
entirely through clio-core context, no in-memory handoff.

This is the cross-process path. Embedding clio-core (`chimaera_init(kClient, True)`)
gives every process its *own* runtime — great for a single node, useless across
processes. To share, you run a **`clio_run` daemon** and every client attaches to it
(`chimaera_init(kClient, False)`). `CTEStore` does that when `CLIO_CTE_WITH_RUNTIME=0`.
(clio-core itself is proven at 64 procs/node × ~1.2k nodes — this is the same path.)

## Run it

```bash
# 0) point at the clio-core daemon binary's libs (ships in the venv)
PKG=$(uv run python -c 'import iowarp_core,os;print(os.path.dirname(iowarp_core.__file__))')
export LD_LIBRARY_PATH="$PKG/../iowarp_core/lib:$PKG/../iowarp_core/bin:$LD_LIBRARY_PATH"
BIN="$PKG/../iowarp_core/bin"

# 1) start the shared clio-core runtime (one per node) — leave it running
CTP_LOG_LEVEL=error "$BIN/clio_run" start &

# 2) start a WORKER clio (attaches to the daemon; runs a real ALCF expert)
export CLIO_CTE_WITH_RUNTIME=0
export CLIO_LM_PROVIDER=argonne CLIO_RUN_LIVE=1
export CLIO_LM_API_BASE=https://inference-api.alcf.anl.gov/resource_server/sophia/vllm/v1
export CLIO_LM_MODEL=openai/gpt-oss-120b
uv run python examples/clio_to_clio/worker.py demo_ &

# 3) run a PARENT clio (a different process) — it delegates through clio-core
uv run python examples/clio_to_clio/parent.py demo_

# 4) stop the daemon when done
"$BIN/clio_run" stop
```

Expected: the parent prints `CROSS_PROCESS=True` and the worker-expert's answer (the
tool-derived value `42.7`), proving the two processes shared clio-core and the
delegation crossed between them.

## What carries it

- `runtime/cee_transport.py` — `CEEMailbox` + `CEEExpertInvoker`: the request/result
  cross through clio-core context blobs; parties share only the store.
- `arc/storage.py` — `CTEStore` with `CLIO_CTE_WITH_RUNTIME=0` attaches to the daemon.

On a cluster the same store spans nodes (the daemon's networking), so this identical
code is cross-machine — the only remaining step toward the distributed plane (#659/#665).
