# Running a parallel CLIO instance — rationale & reference

A CLIO grind is long-running and stateful: it holds a gact server on a port, an
ARC store on disk, staged NDP files, traces in `runs/`, and an open git branch.
To grind a *new* case while another grind is in flight, you must isolate every
one of those, or the two will corrupt each other (mixed ARC memory, clobbered
staged files, port bind failures, git index collisions).

## The five isolation axes

| Axis | Control | Why it must be unique |
|------|---------|-----------------------|
| Code / branch | `git worktree add ../clio-<case> -b feat/<case>` | The other grind has its own dirty working tree + branch. A worktree gives you an independent checkout sharing the same `.git`. |
| Server port | `--port <N>` on the gact server | One process per port. The launcher default is **17800**; the agent-test SUT default is **17960**; raw uvicorn entrypoints default to **8100** (gact) / **8000** (api). Pick something else, e.g. 17970. |
| ARC / state | `CLIO_DATA_DIR=$PWD/.clio_agent` | Default is `.clio_agent` relative to cwd. Two instances sharing it interleave conversations, invocations, and cache. |
| File sandbox | `CLIO_ALLOWED_ROOTS=$PWD:/tmp` | Gates tool file access and where NDP staging lands. Keep each grind's staged data separate. |
| Artifacts | `CLIO_ARTIFACTS_ROOT=$PWD/.clio/artifacts/geo` | Geo renders (the "wow" output). Don't overwrite the other grind's maps. |

## Ports in use (check before you pick)

```bash
cat ~/.local/share/clio/clio-server.pid 2>/dev/null     # the launcher-managed instance
ss -ltnp 2>/dev/null | grep -E ':(8000|8100|17800|17960|17970)\b' || echo "those ports are free"
```

## Full parallel session

```bash
# --- one-time setup ---
cd /home/jcernuda/clio-agent
git worktree add ../clio-<case> -b feat/<case>
cd ../clio-<case>
uv sync --extra dev --extra optimizers

# --- per-shell env (export in every shell that runs the server or the tests) ---
export CASE_PORT=17970
export CLIO_DATA_DIR="$PWD/.clio_agent"
export CLIO_ALLOWED_ROOTS="$PWD:/tmp"
export CLIO_ARTIFACTS_ROOT="$PWD/.clio/artifacts/geo"
export CLIO_GACT_URL="http://127.0.0.1:$CASE_PORT"

# --- shell A: the isolated gact server (leave running) ---
uv run uvicorn clio_agent.gact.app:app --host 127.0.0.1 --port "$CASE_PORT"

# --- shell B: drive it ---
export CLIO_RUN_LIVE=1
uv run pytest tests/test_real_cases/test_<case>.py --provider argonne_metis \
  -m "real_case and live" -s
```

## Provider / live-run notes

- Guardrail cell for grinds: `argonne_metis` / `gpt-oss-120b`. Override the cell
  matrix with `CLIO_AGENTTEST_CELLS` or pin with `pytest --provider/--model`.
- `CLIO_RUN_LIVE=1` is required or the live tests skip (see
  `tests/test_real_cases/conftest.py`).
- ALCF auth is a Globus token shared via keyring — two concurrent grinds can both
  use it; watch ALCF rate limits, not auth.
- Install the new case's blueprint at **`scope: workspace`** so it lands in
  `$PWD/.clio/agent-blueprints` and never touches the global registry or the
  other grind's pack.

## Teardown

```bash
# stop the server (Ctrl-C in shell A), then, once the case is merged:
cd /home/jcernuda/clio-agent
git worktree remove ../clio-<case>     # add --force if it has uncommitted scratch
```

## Things that are NOT safe to share

- The same `CLIO_DATA_DIR` (ARC corruption / mixed memory).
- The same port (second server fails to bind, or you talk to the wrong agent).
- The same git working tree/branch (use a worktree).
- The launcher pidfile `$CLIO_PREFIX/clio-server.pid` — only an issue if you use
  the `clio` launcher instead of raw uvicorn; give each a distinct `CLIO_PREFIX`.
