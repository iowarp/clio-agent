# GOAL — Distributable expert runtime

> Predecessor: this file previously held the **ARC live context plane** goal — now complete
> and preserved on branch `feat/arc-live-context-plane`. That data plane is the foundation
> this execution-plane work builds on.

Tracking epic: **#667**. Branch: `feat/distributable-expert-runtime`, worktree
`/home/jcernuda/clio-distributable-experts`, based on `feat/arc-live-context-plane`.

## North star

Reach **detangled (local + detached) expert-invocation semantics on a single box**, with the
detached path proven over a loopback transport. Then taking this to a GPU cluster, the only
remaining step is swapping the loopback for clio-core's cross-machine transport and building
the distributed context (#659, #665). **Build and test everything reachable without the
cluster, here.**

## Done condition (what "reached" means)

An expert can:
1. **Run on its own declared model** — `(provider, model)` from its `.md` resolves to the
   right endpoint, including a provider distinct from the run default, with its own context
   window. _(a #668, a.2 #669)_
2. **Be launched async and monitored** — spawn→handle, `status`/`poll_output`,
   `wait(until/timeout)`, `on_complete` (model-stream event + side-effect callback),
   `cancel`, `resume`; a parent launches a background child, continues or waits by policy,
   and folds structured completion back into the same session lineage. _(c #670, b #441)_
3. **Be invoked through a transport-abstracted boundary** — an `ExpertInvoker` with an
   in-process impl (parity, regression-gated) AND a detached loopback impl (request
   serialized, child events folded back), so swapping the transport is the only cluster
   step. _(e #671 — the hinge)_

Reached when (3) holds with both impls green, parity-tested, and the loopback path validated
live on two ALCF providers.

## Capability graph (dependencies, not phases)

```
(a) #668 ──┐
(a.2) #669 ┴──> heterogeneous experts (each its own model)

(c) #670 ──> (b) #441   async / background experts
                  └───┐
(e) #671 ─────────────┴──> detangled local + detached invocation   ◀── north star

              ── on the GPU cluster ──>  (d) #659  +  distributed context plane #665
```

Natural first beat: **(a) + (a.2)** — mostly built, independently testable, the prerequisite
for heterogeneous teams. Then (c) → (b) → (e). The order is a dependency graph; let the
trace drive what's next, not a schedule.

## Testing strategy ("full faith before deploy")

- **Live = two ALCF providers, each serving its own model.** Heterogeneity proven
  endpoint-wise; no local router; the local machine stays free for other systems. The
  single-endpoint llama.cpp/LM Studio router is documented (a.2) but off the test path.
- **Per-expert model:** offline — two experts, two declared models, assert each turn hits the
  LM boundary with its own model (`lm.history` / PromptRecorder) and its own context window.
  Live — same on two ALCF providers, end-to-end.
- **Monitor/async:** unit — handle lifecycle, status transitions, `wait` timeout, `cancel`,
  notify (both channels), `resume` preserves context. Integration — a real long-running
  expert turn, child completion folds back, status surfaced without pretending the parent
  finished.
- **Invocation boundary:** parity — in-process invoker ≡ current behavior (result-identical,
  regression-gated) across blueprint + tool-user paths. Loopback — child over the detached
  transport folds events/results back; live on two ALCF providers.

## Principles (CLAUDE.md superseding rules — these win)

1. **No deterministic decision-making in core.** Parent model routes/decides via structured
   output; clio carries results, executes handoffs, re-asks on a missing one. Async/detached
   preserve this — a detached expert is still parent-driven, never clio-heuristic-driven. No
   prose-keyword "done"/"pending" detectors.
2. **Fix the root in code/data-flow, not by bolting constraints onto expert prompts.**
3. **Trace-driven driver:** run the target test, read the FULL trace, hypothesize one cause,
   probe cheaply (~30s) before a multi-minute rerun, fix the root, one change per rerun.
4. **DSPy is the engine + reference** (`docs/ref/dspy/`); the live fold accepts raw
   `SemanticEvent`s from any producer (`arc/live.py`) — that's the seam detached invocation
   rides on.

## Infra traps (from the ARC build — avoid re-hitting)

- **Worktree venv is separate.** Already synced here: `uv sync --extra dev --extra optimizers
  --extra argonne` + `git submodule update --init external/clio-agent-marketplace`. Always
  test via `uv run python -m pytest` (never bare `uv run pytest` — it can silently fall back
  to another worktree's venv; coverage paths pointing elsewhere are the tell).
- **ALCF "just works"** with the `argonne` extra (globus-sdk) installed; the Globus token is
  auto-managed on this machine. Live env: `CLIO_LM_PROVIDER=argonne`,
  `CLIO_LM_API_BASE=...alcf.anl.gov/.../vllm/v1`, `CLIO_LM_MODEL=openai/gpt-oss-120b`,
  `CLIO_RUN_LIVE=1`. For multi-model: a second ALCF provider/model alongside.
- **No phased plans.** Capture context and dependencies; let the build be goal-driven.
