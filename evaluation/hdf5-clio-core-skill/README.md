# clio-core HDF5 ingest — benchmark, skill, and A/B evidence

> The benchmark harness, fixture generators, and raw result tables also live in
> `clio-core/benchmarks/hdf5-ingest/`. This folder holds the agent-side work:
> the data-backed skill's A/B evaluation and the routing study.


Measured how clio-core (CTE/CAE) handles HDF5 ingest vs native h5py, distilled the
findings into an advisory HDF5-expert skill in `clio-agent`, and tested whether the
skill changes the agent's answers.

## Artifacts by purpose

**Benchmark** — clio-core ingest/read vs native h5py
- `run_bench.py` — runner (segments: ingest/locate/fetch/decode; cold vs warm).
- `analyze.py` — results → comparison tables.
- `gen_fixtures.py`, `gen_extra_fixtures.py` — fixtures (access-pattern matrix; object-count sweep; large single-dataset). Output in `fixtures_200mb/`.
- `results.json` (matrix), `results_expansion.json` (sweep+large), `results_all.json` (merged, 62 cells), `results_analysis.txt` (formatted tables).
- `BENCHMARK_DESIGN.md` — methodology + op/measurement contract.

**Skill** (lives in `clio-agent`)
- `clio-agent/src/clio_agent/experts/hdf5_skills/hdf5-clio-core-ingest/SKILL.md`.

**Skill A/B — Tier-2 expert (controlled skill evidence)**
- `skill_ab_demo.py`, `skill_ab_results.json`, `SKILL_AB_DEMO.md`.
- Expert called directly, skill on vs off (Ollama `granite3.1-dense:8b`, temp 0).

**Real-agent A/B — Tier-1 orchestrator (routing finding)**
- `e2e_advisory_ab.py`, `e2e_advisory_ab_results.json`, `E2E_ADVISORY_AB.md`.
- Full `ClioAgent`, skill on vs off (Groq `gpt-oss-120b`). Shows the skill helps when the planner delegates — but delegation is unreliable.

**Supplementary (skill-independent capability check)**
- `e2e_orchestrator_demo.py`, `e2e_orchestrator_results.json`, `E2E_ORCHESTRATOR_DEMO.md` — orchestrator executes an h5repack rechunk end-to-end. Demonstrates the system can act; does not exercise the skill.

**Environment / reproduction**
- `_tmp_wheelhouse/` — offline wheels to rebuild h5py against the container's HDF5 2.1.1 (strict parity).
- `hello_cee.py` — original CEE smoke script (pre-existing).

## Key findings
1. clio-core read cost scales ~linearly with dataset **count** (~100–200× per-object vs h5py); `locate` (per-object metadata) dominates.
2. Bundling **never amortizes for read speed** (clio-core warm > native cold, up to 2 GB); cost is per-object/per-chunk, not per-byte.
3. Skill **measurably corrects** the expert's advice (Tier-2 A/B).
4. In the real agent, value is realized **only when the planner delegates** to the expert; delegation is nondeterministic and the soft levers (keywords/description) don't reliably fix it.

## Reproduce
```bash
# benchmark + analysis (any python with h5py/numpy)
python run_bench.py --out results.json && python analyze.py --results results_all.json

# Tier-2 A/B (Ollama):   CLIO_LM_PROVIDER=ollama CLIO_LM_MODEL=granite3.1-dense:8b ... skill_ab_demo.py
# Tier-1 A/B (Groq):     CLIO_LM_MODEL=openai/gpt-oss-120b ... e2e_advisory_ab.py
# (run with the clio-agent venv; see each script's header)
```
