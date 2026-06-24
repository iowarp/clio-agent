# GACT package architecture — "where things go"

> Status: target architecture for the gact decomposition.
> Tracking issue: iowarp/clio-agent#714.

This document is the contributor-facing map for the `clio_agent.gact` package.
It exists because `gact/app.py` grew into a ~24k-line monolith that mixes the
turn engine, agent runtimes, semantic emission, HTTP routing, and the FastAPI
assembly in one file. The architecture pass (#714) carves that monolith into
small, single-responsibility modules. When you add code, put it in the module
that owns the concern below — do **not** add it back to `app.py`.

## Target module-ownership table

| Module / package | Owns |
| --- | --- |
| `app.py` | **Thin assembly + re-export shim.** Builds the FastAPI app (`build_app`), wires routers/middleware, exposes `main`, and re-exports the stable symbol surface (public + private test seams) so importers keep working as code moves out. No business logic. |
| `context.py` | `TurnContext` / `ExpertContext` dataclasses and the **single runtime contextvar** that carries the active context object through a turn. |
| `runtime/` | The turn engine and its collaborators: turn orchestration/lifecycle, delegation, streaming, context frames, and cost accounting. |
| `agents/` | Agent runtimes and their building blocks: `react`, `blueprint_module`, `tool_user`, `prompt_user`, `signatures`, `fanout`, `repair`. |
| `emit/` | Semantic event emitters — the code that publishes onto the ARC event stream / semantic-event highway. |
| `routes/` | HTTP routing: **one `APIRouter` per resource**. Routers are imported and mounted by `app.py`; they contain no engine logic. |
| `permissions.py` | Permission gates and policy evaluation. |
| `catalog.py` | Agent / expert / tool catalog construction and lookup. |
| `resolution.py` | Runtime resolution of dynamic agents, blueprints, and signatures. |
| `lm_runtime.py` | LM runtime concerns (binding, token refresh, provider plumbing) used by the agent runtimes. |
| `workflow_state/` | Workflow-state modelling, persistence, and projection. |

Existing modules (`agent_blueprints.py`, `expert_packs.py`, `user_agents.py`,
`messages.py`, `sessions.py`, `events.py`, `semantic_events.py`, `scheduler.py`,
`workspaces.py`, `workspace_scope.py`, `types.py`) keep their current
responsibilities and are folded into the table above as their concerns settle.

## The two invariants

These are the load-bearing rules the decomposition must preserve. If a change
would violate one of them, stop and reconsider the design.

1. **ONE runtime contextvar carrying the context object.**
   There is a single `contextvar` that holds the active context object
   (`TurnContext` / `ExpertContext`) for the current turn. Context is read and
   threaded through that one variable — **not** through a scattering of
   per-concern contextvars. New per-turn state belongs as a field on the context
   object, not as a new module-level contextvar.

2. **ONE ARC event stream with two clearly-owned projections.**
   There is a single ARC event stream. It has exactly two projections, each with
   a clear, separate owner — and **neither is "the live context plane"**:
   - **`SegmentStore`** — the working-set / prompt scratchpad projection (what
     gets assembled into prompts).
   - **`LiveRuntimeContext`** — the trace projection (the folded, observable
     view of the run for debugging and inspection).
   Do not introduce a third projection that quietly becomes a parallel source of
   truth, and do not let either projection start mutating the event stream.

## Rules for new modules

When you add or split a module in `clio_agent.gact`:

- **< 800 lines.** New modules must stay under the 800-line cap. The cap is
  enforced (warn-only for now, then blocking) by `scripts/check_file_size.py`.
  `app.py` is temporarily allowlisted while it is decomposed.
- **No classes defined inside functions.** Lift types to module scope so they
  are importable, typeable, and testable. Enforced by
  `scripts/check_no_class_in_function.py` (warn-only for now, then blocking).
- **Respect import layering.** Lower layers must not import upward:
  `app.py` (assembly) → `routes/` → `runtime/` + `agents/` + `emit/` →
  `context.py` / `catalog.py` / `resolution.py` / `permissions.py` /
  `lm_runtime.py` / `workflow_state/`. Routers depend on the engine; the engine
  does not depend on routers. Avoid cycles; if you need a symbol from a higher
  layer, the dependency probably points the wrong way.

See `scripts/check_file_size.py` and `scripts/check_no_class_in_function.py` for
the enforced guardrails, and the CI `warn:` steps in `.github/workflows/ci.yml`
that run them. Tracking issue: iowarp/clio-agent#714.
