# Unified concurrency model — #735 / #758 / #770

> **Status:** design approved-in-principle, implementation not yet started.
> Produced by the `unified-concurrency-design` workflow (propose ×3 → synthesize →
> adversarial critique → finalize) and adversarially reviewed. Supersedes the
> scattered A1/A2/Wave-B patching approach: the three issues are ONE class
> ("process-global mutable runtime state read across apps/threads") and get one
> uniform model, not independent patches.
>
> **Owner decisions taken (2026-07-02), both revisitable:**
> 1. **Direction:** proceed — implement trace-driven, one step at a time, each with
>    a failing-first test and a clean-context adversarial verify.
> 2. **App-less tool-call policy:** reason-logged fallback (§7 decision A) — the
>    baseline-safe default (RULE 2: the app-less CLI must keep working). Fail-closed
>    / differentiated is deferred until traces show whether any real out-of-band
>    tool call exists.
>
> **Implementation note found while starting Step 1 (feeds §5 step 1 / #813):**
> DSPy's `ParallelExecutor` **resubmits stragglers** (`utils/parallelizer.py:194`)
> reusing the same work item. If each nanoagent module is wrapped in a SINGLE
> captured `contextvars.Context`, a slow worker overlapping its own resubmit would
> call `Context.run()` on the same Context twice concurrently →
> `RuntimeError: cannot enter context: already entered`. The rail fix must give each
> execution its own context copy (or disable resubmit via `timeout=0`, since we do
> not want hung nanoagents resubmitted anyway). Not yet implemented.

---

Both design-flipping claims are confirmed in the code:

- `nanoagent.py:141-150` — the `#813` warning is real and explicit: `dspy.Parallel` forwards only DSPy's `thread_local_overrides` and does **not** copy `contextvars.Context`, so `active_app()` cannot reach nanoagent workers; today they fall back to the process-globals.
- `builders.py:907` — `_blueprint_runtime_signature(agent_def)` takes **only** `agent_def`; `_route_app = _ctx.active_app()` at `:1019`. There is no explicit `app` to "prefer." The children cache and the briefing cache are genuinely different callsites.

Both findings stand. Here is the final design with the critique folded in.

---

# Unified Runtime-State Design — FINAL (critique folded)

## 0. What changed from the synthesis (disposition of the 5 critiques)

| # | Critique | Disposition |
|---|---|---|
| 1 | Keystone rail is broken on the nanoagent/`dspy.Parallel` path; deleting the globals removes the net #813 counts on | **FIXED (root, not fallback).** Fix the rail in `spawn_many` (wrap each worker in the captured `copy_context().run`) as a **precondition** for any global deletion. Globals collapse to **one** reason-logged fallback bundle, not zero. |
| 2 | App-less resolve returns silent structured-empty → silent security downgrade, violates no-silent-fallback | **FIXED.** `current_tool_runtime()` app-less path resolves the single retained fallback bundle and **emits a structured `tool_runtime_appless_fallback` reason**; when even that is empty it emits `tool_runtime_unresolved`. No silent drop of a permission gate, ever. |
| 3 | §5's "prefer explicit app" is inapplicable to the children cache (`_blueprint_runtime_signature` has no `app` param) | **FIXED (split).** Briefing cache → explicit-`app` `per_app_dict` (it *has* `app`). Children cache → thread `app`/`session_id` in explicitly from callers that have it; where genuinely app-less, keystone-in-turn is the sole source, **gated on a live-trace proof** — and the collapse-to-`finish` mode is accepted as a *known* degradation, not sold as "strictly better." |
| 4 | Keystone broadens `active_session_id()` for the whole turn; only empty-gated readers audited | **ACCEPTED w/ added audit.** Bidirectional audit of `lm_activity.py:90/207/288` and the suppressed→firing emitters is a required step; the telemetry-volume change is intended but must be diffed on a trace. |
| 5 | "One principle, three mechanisms" oversells facets 3–4 | **ACCEPTED (reframed).** Two mechanisms, not one: (A) app-resolution for app-lived state (hooks, caches); (B) explicit-owner + serialization for irreducibly-global resources (hook pool, LM-bind), with the eventbus as reference. Facets 3–4 are independent bugs sharing discipline (B), not the `active_app()` mechanism. |

Confirmed-correct and unchanged from the synthesis: layering proof (§9), the `_ACTIVE_TOOL_WORKSPACE_ROOT` carve-out (§4.3), eventbus untouched, `notify_global_tool_observer` is test-only, keystone bare-set leak semantics are sound.

---

## 1. The unified principle

Per-turn/per-app runtime state has exactly **one home — `app.state` — reached on worker threads by resolving the live turn's app through `_ctx.active_app()`**, which the keystone makes reliable on every dispatch path *that preserves the `copy_context` rail* (the one path that doesn't — `dspy.Parallel` in the nanoagent tier — is repaired at its root so the invariant is actually universal, not assumed). The low `tools` layer owns only an inversion-of-control **slot** (a frozen data shape plus one function pointer) that gact fills once with a stateless resolver; nothing per-app is ever pushed into the low layer. Two categories, two mechanisms: **(A)** app-lived state (tool hooks, expert caches) is *resolved* from `active_app().state`; **(B)** irreducibly process-wide resources (`os.environ`/`dspy.settings`, a shared thread pool, the event loop) are not shared lock-free — each gets one explicit owner plus a serialization/affinity seam, with the eventbus as the reference. Every app-less resolve returns a *structured, reason-emitting* result — never a sibling app's value, and never a silently dropped safety hook.

---

## 2. The core abstraction

**Name:** `ToolRuntimeHooks` (the port/shape) + `_TOOL_RUNTIME_RESOLVER` (the slot) + `current_tool_runtime()` (the reader).

**Where it lives:** `src/clio_agent/tools/execution.py` (the low layer). The gact-side *adapter* lives in a **new owner module** `src/clio_agent/gact/runtime/app_state.py` (honors the no-accretion rule — nothing new is bolted onto `globals.py` or `build_app`).

**Exact shape / signatures — `tools/execution.py`:**

```python
@dataclass(frozen=True)
class ToolRuntimeHooks:
    permission_gate: Callable[[str, Mapping[str, Any]], str] | None = None
    tool_observer: ToolObserver | LegacyToolObserver | None = None
    tool_interceptor: Callable[[str, Mapping[str, Any]], Any | None] | None = None
    cancellation_checker: Callable[[], bool] | None = None

# One resolver slot (installed once by gact) + one reason-logged fallback bundle.
_TOOL_RUNTIME_RESOLVER: Callable[[], ToolRuntimeHooks | None] | None = None
_FALLBACK_TOOL_RUNTIME: ToolRuntimeHooks = ToolRuntimeHooks()   # single retained net

def set_tool_runtime_resolver(fn: Callable[[], ToolRuntimeHooks | None] | None) -> None:
    global _TOOL_RUNTIME_RESOLVER
    _TOOL_RUNTIME_RESOLVER = fn

def set_tool_runtime_fallback(hooks: ToolRuntimeHooks) -> None:
    """Last-installed app's hooks, used ONLY when no app resolves (out-of-band)."""
    global _FALLBACK_TOOL_RUNTIME
    _FALLBACK_TOOL_RUNTIME = hooks

def current_tool_runtime() -> ToolRuntimeHooks:
    r = _TOOL_RUNTIME_RESOLVER
    resolved = r() if r is not None else None
    if resolved is not None:
        return resolved
    # App-less: fall back, but LOUDLY (no-silent-fallback rule).
    fb = _FALLBACK_TOOL_RUNTIME
    _emit_tool_runtime_reason(
        "tool_runtime_appless_fallback" if fb.permission_gate or fb.tool_observer
        else "tool_runtime_unresolved"
    )
    return fb
```

`_emit_tool_runtime_reason` is a thin structured-reason emitter modeled on the `stream_fallback` catalog (a typed reason recorded per session, queryable after the fact). It carries the tool name and the resolve site; it does **not** import gact (it writes to the same low-layer audit sink `lm_activity`/stream-audit already reads).

**Why one installed resolver rather than a per-turn ContextVar (P3):** once the keystone lands *and the nanoagent rail is fixed*, `active_app()` is authoritative on every executor thread, so a per-turn binding of hooks is redundant work that rides the same rail and degrades identically. The stateless resolver dispatches on `active_app()` at call time; one install serves N apps.

**How the layering stays acyclic:** `tools/execution.py` imports nothing from `clio_agent.gact` — it defines a stdlib dataclass, a `Callable` slot, two setters, and *calls* an opaque callable. Direction is unchanged from today's `set_global_*` (gact → tools); only the arity (one resolver vs four pushes) and pull-vs-push change. Adapter edge set: `tools/execution ← gact/runtime/app_state ← gact/app`. Strictly acyclic (verified: `execution.py` imports only `conf`, `errors`, `tools.file_policy`, stdlib, dspy, fastmcp).

---

## 3. The keystone change

In `turn.py::_run_turn_in_background`, replace the two bare sets at `:628-629`:

```python
# was:
_ctx.set_turn_id(turn_id); _ctx.set_trace_id(trace_id)
# now:
_ctx.set_turn_identity(app=app, session_id=sid, turn_id=turn_id, trace_id=trace_id)
```

`set_turn_identity` already exists at `context.py:213` and is confirmed **dead code** (no callers) — it is live, tested-shaped, and does a tokenless bare set of the whole `TurnContext`. Leak semantics are identical to the `turn_id`/`trace_id` bare sets it replaces: `_run_turn_in_background` is dispatched as a tracked `asyncio.Task` (`turn.py:3495`), each Task gets its own `copy_context()` copy, so the un-reset set cannot cross turns/apps. Every later `run_in_executor` snapshot now carries `turn.app` + `turn.session_id`.

**Precondition it depends on (the critique's decisive finding):** the keystone only helps threads seeded from the turn's `copy_context()`. `dspy.Parallel` (nanoagent, Tier 3) submits to a plain pool that drops `contextvars.Context` (`nanoagent.py:141-150`, confirmed). So the keystone is **not** universal until `spawn_many` (`nanoagent.py:151`) wraps each worker in the captured context:

```python
captured = contextvars.copy_context()
parallel = dspy.Parallel(num_threads=num_threads)
raw_results = parallel([(WrapCtx(captured, agent), kw) for agent, kw in pairs])
# WrapCtx.__call__ runs the module via captured.run(agent, **kw)
```

This is the fix `#813`'s own warning prescribes. **It is Step 1 of the plan and a hard precondition for any global deletion** — without it, deleting the globals leaves nanoagent tool calls with no observer/gate/cancellation.

**Independent payoff (land even alone):** `_emit_react_step_event` / `_emit_expert_lifecycle_event` (`globals.py:547/607`) currently `return` when `active_app() is None`, so on the main-orchestrator path they emit nothing. The keystone closes that highway gap.

---

## 4. Per-site plan

### Site 1 — #735 tool-runtime hooks (`tools/execution.py`, `gact/runtime/globals.py`, `gact/tool_observer.py`, `gact/app.py`)

**Changes:**
- `tools/execution.py`: add `ToolRuntimeHooks`, `_TOOL_RUNTIME_RESOLVER`, single `_FALLBACK_TOOL_RUNTIME`, `set_tool_runtime_resolver`, `set_tool_runtime_fallback`, `current_tool_runtime` (§2). `SyncMCPToolExecutor.call_tool` (~`:661-663`, `:700`) collapses to one resolve, instance overrides still winning:
  ```python
  hooks = current_tool_runtime()
  permission_gate = self._permission_gate or hooks.permission_gate
  tool_observer   = self._tool_observer   or hooks.tool_observer
  cancellation_checker = hooks.cancellation_checker
  tool_interceptor     = hooks.tool_interceptor
  ```
  `notify_global_tool_observer` (`:225`) re-points to `current_tool_runtime().tool_observer` (test-only caller surface — cosmetic for `src`).
- `gact/runtime/app_state.py` (**new**): `resolve_tool_runtime()` reads `active_app().state.pending_*` into a `ToolRuntimeHooks`, returning **`None`** (not empty) when app-less so `current_tool_runtime` takes the reason-logged fallback path. Plus the shared `per_app_dict(name)` helper (Site 2).
- `gact/tool_observer.py:194-221`: keep only the `app.state.pending_* =` stamps (all four, so absent ⇒ `None` cleanly); additionally call `set_tool_runtime_fallback(ToolRuntimeHooks(...))` here so the single retained net = last-installed app's hooks. Delete the four `set_global_*` calls.
- `gact/app.py`: `build_app` calls `set_tool_runtime_resolver(resolve_tool_runtime)` once (idempotent). Cache imports (`:90-91`) and the four `set_global_*(None)` teardown (`:1278-1287`) deleted.
- `gact/runtime/globals.py::_tool_session_context`: the entire hook block **and** the `app` parameter are deleted; it collapses to `set_tool_session_id(sid)` + `tool_workspace_context(root)`.

**Deleted:** the **three** non-fallback globals + their concept as four separate globals (collapsed to one `_FALLBACK_TOOL_RUNTIME` bundle); all four `set_global_*` setters + `__all__` entries (`:1066-1069`); the four `_CTX_*` ContextVars; the `_UNSET_HOOK` sentinel; `tool_runtime_hooks_context`; the four `_active_*` resolvers; the `,app` args at the four `_tool_session_context` callsites in `turn.py`; the redundant `_gact_app_context(app)` wrappers (`turn.py:1120/2011`).
**Kept:** `_ACTIVE_TOOL_WORKSPACE_ROOT` / `tool_workspace_context` / `get_active_tool_workspace_root` — read on the app-less CLI path (`agent.py:385`, `file_policy.py:69`); folding it into an `active_app()`-keyed bundle would regress CLI grounding to `""` (§4.3 of synthesis, confirmed correct).

### Site 2 — #770 expert caches (`gact/runtime/globals.py`, `gact/agents/builders.py`, `gact/agents/composition.py`)

**Split into two, because the callsites differ:**

- **Briefing cache** (`composition.py:_orchestrator_identity_briefing`, `:255`): it *receives* `app`. Replace `_ORCHESTRATOR_BRIEFING_CACHE[id]` with `per_app_dict("orchestrator_briefing", app=app)[id]` keyed on that explicit app — correct at construction time *and* in-turn.
- **Children cache** (`builders.py:_blueprint_runtime_signature`, `:907`, cache use at `:1030-1038`): **no `app` param exists** (`_route_app` *is* `active_app()`). Two-part fix: (a) add an optional `app: AppLike | None = None` param and thread it in from every caller that has one, storing/reading on that app's `per_app_dict("expert_children")`; (b) where a caller is genuinely app-less, keystone-in-turn (`active_app()`) is the sole source — **gated on the §6 live-trace proof**. Delete `_EXPERT_CHILDREN_CACHE` from `globals.py`.

**Deleted:** `_EXPERT_CHILDREN_CACHE`, `_ORCHESTRATOR_BRIEFING_CACHE` (both process-global dicts, `globals.py:111-117`).
**Explicitly accepted risk (not sold as "strictly better"):** when a children-cache consume is genuinely app-less, `per_app_dict` returns a structured empty → `next_expert` collapses to `Literal["finish"]` → an orchestrator can finish on step 1 with no delegation. This is *isolated and deterministic* (never a sibling app's child id), but it is a **real failure mode**, not an improvement. The design forbids shipping it on faith: Step 5 requires a trace showing every orchestrator signature is (re)built *within* a turn; any app-less-only build must have `app` threaded in explicitly (part (a)).

### Site 3 — #770 hook-timeout pool (`runtime/hooks.py:300-310`)

**Changes:** delete the shared `ThreadPoolExecutor(max_workers=4)` (+ its `DisabledHookRegistry` twin). `future.cancel()` is confirmed a no-op once a hook is running, so one wedged hook pins all four workers and then every hook times out. Replace with a per-invocation daemon thread (`clio-hook-<event>`), `join(timeout_s)`; on overrun, abandon the daemon and raise `TimeoutError` with a structured `hook_timeout_abandoned` reason (hook path + event). Mirrors `builders.py::_run_external_mcp_tool_sync`.
**Deleted:** the shared pool, the `future.cancel()` reliance.
**Accepted risk:** unbounded thread count under a pathological hook storm — bound with a semaphore only if a trace shows it; strictly better than deterministic 4-worker exhaustion regardless.

### Site 4 — #770 LM-bind (`gact/routes/providers.py:1099-1102`, `:1230-1232`, `_apply_lm_provider:677`)

**Changes:** introduce one `app.state.lm_bind_lock` (`asyncio.Lock`) around the whole snapshot → mutate-`os.environ` → reconfigure-`dspy` → restore critical section for **every** provider (today's `lm_config_task` guard covers only some). Drop the nested `run_in_executor(None, lambda: asyncio.run(_apply_lm_provider(...)))` on both paths; `await _apply_lm_provider(...)` on the serving loop. Its heavy sub-steps already offload via their own `run_in_executor` (`:948/:1004`) — those **must stay** offloaded or the bind blocks the loop.
**Deleted:** the nested `asyncio.run` on both bind paths; the partial `lm_config_task` guard.
**Accepted:** `os.environ`/`dspy.settings` remain process-global by necessity (LiteLLM/dspy read them); they gain a single serialized owner — mechanism (B), same *discipline* as the eventbus but **not** the `active_app()` mechanism. Keep the lock tight/non-reentrant to avoid a re-entrant `_apply_lm_provider` deadlock.

### Site 5 — #758 eventbus (`gact/events.py`)

**No change.** Reference implementation for mechanism (B): binds its owning loop on first `subscribe`, bridges foreign-thread publishes via `loop.call_soon_threadsafe`, per-instance on `app.state.bus`. The Site 4 fix removes its last foreign-loop publisher.

### Fate of PR #812 (`fix/735-tool-hooks-contextvar`)

**Superseded and folded — one branch, one coherent diff.** #812 is correct on all reachable paths but is the one-off this program unifies: a 5th/6th per-turn ContextVar and a *second materialization* of hooks that already live on `app.state.pending_*`, layered over four *retained* globals, with `_UNSET_HOOK` existing solely to stop a per-turn `None` from masking those globals. This design **deletes #812's entire added mechanism** — the four `_CTX_*` ContextVars, `_UNSET_HOOK`, `tool_runtime_hooks_context`, the four `_active_*` resolvers, and the `app`-threaded `_tool_session_context` signature — *and* collapses the four retained globals to **one** reason-logged fallback bundle. #812's behavior (per-app hook isolation on every turn path, no leak to a sibling's global) is preserved through the keystone riding the existing `copy_context` rail plus the nanoagent-rail fix. The sentinel's raison d'être evaporates: with `app.state.pending_*` the single always-stamped source, "absent ⇒ `None` ⇒ no hook" is unambiguous. **Mechanics:** land the keystone + rail fix + unified seam as follow-on commits **on #812's branch**, removing #812's additions, so the branch's final diff *is* the unified design. The #735 regressions #812 introduced are re-pointed at the resolver path (not reverted). Do **not** merge #812 to develop and then re-touch it.

---

## 5. Ordered implementation steps (one change per rerun, keystone-first, smallest-first)

1. **Nanoagent rail fix** (`spawn_many`, `nanoagent.py:151`) — wrap each `dspy.Parallel` worker in `captured = copy_context(); captured.run(...)`. **Failing-first:** a two-app test that drives a nanoagent tool call and asserts `active_app()` (and workspace root) resolve to the *spawning* app in the worker — fails today (Context dropped), passes after. This is the precondition; land before any deletion.
2. **Keystone** — `set_turn_identity` at `turn.py:628-629`. Verify `sid` is in scope at that line first. **Failing-first:** assert `active_app()` non-None inside a `run_in_executor` snapshot on the **orchestrator** path, and that `react.step`/`expert.lifecycle` events carry a session on the main path (`globals.py:547/607`). Then run the §7 bidirectional `active_session_id` audit before proceeding.
3. **Tools-layer seam + adapter** — `ToolRuntimeHooks` + resolver slot + single fallback + `current_tool_runtime` (with reason emission) in `execution.py`; `gact/runtime/app_state.py` (`resolve_tool_runtime` + `per_app_dict`); `build_app` install; migrate `call_tool` + `notify_global_tool_observer`. Do **not** delete the globals yet. **Failing-first:** the two-app hooks-isolation integration test (§6.1) on the *orchestrator* path; plus an app-less-resolve test asserting a `tool_runtime_appless_fallback` reason is emitted (not a silent empty).
4. **Delete #812's additions + the globals → single fallback** — remove the four `_CTX_*` vars, `_UNSET_HOOK`, `tool_runtime_hooks_context`, the four `_active_*` resolvers, the four `set_global_*`, collapse to `_FALLBACK_TOOL_RUNTIME`; re-point #812's #735 regressions at the resolver path; delete tests asserting `_CTX_*`/`set_global_*`. Gated on Steps 1+3 green. Regression-check.
5. **#770 caches** — briefing cache → `per_app_dict(app=app)`; children cache → thread `app` param + `per_app_dict`, app-less path gated on the live-trace proof. **Failing-first:** §6.2. **Trace gate:** run a two-app orchestration turn, read `sess_*.json` + `workflow_state`, confirm every orchestrator signature builds in-turn; thread `app` explicitly into any app-less-only build found.
6. **hook-timeout pool** (`hooks.py`) — independent. **Failing-first:** §6.3.
7. **LM-bind lock** (`providers.py`) — independent. **Failing-first:** §6.4.

Eventbus untouched throughout. Everything lands on `fix/735-tool-hooks-contextvar` as follow-on commits.

---

## 6. Test strategy

**Failing-first regressions (one per site):**

1. **Hooks isolation (the keystone integration test):** two `build_app()` instances A/B in one process, distinct sentinel `pending_tool_observer` on each; drive a **real MCP tool call** on a `copy_context()` snapshot taken inside **A's orchestrator turn** (the path that sets no app today) from a `run_in_executor` worker; assert A's observer received it and B's did not. Fails pre-keystone (global reflects last-installed B), passes after. **Add a nanoagent variant:** same two apps, drive the tool through `spawn_many`/`dspy.Parallel`; assert A's gate/observer fire in the worker and B's do not — fails without Step 1, passes after (this is the test the synthesis omitted).
2. **App-less resolve emits a reason:** call `current_tool_runtime()` with no active app; assert it returns the retained fallback **and** a `tool_runtime_appless_fallback`/`tool_runtime_unresolved` reason reached the audit sink (no silent gate drop).
3. **#770 caches:** build the same expert id under A (children `[x,y]`) then B (children `[z]`); assert each `next_expert` Literal is its own app's, never first-writer-wins; plus the app-less-consume case asserts a structured empty (deterministic `finish`), **not** B's children.
4. **Hook pool:** register a hook that sleeps forever; fire 5 concurrently; assert a 6th still times out cleanly (not starved) and a `hook_timeout_abandoned` reason is emitted. Fails on the shared 4-worker pool.
5. **LM-bind:** two concurrent `/providers` binds for different providers; assert final `os.environ`/`dspy.settings` are internally consistent with one winner (no interleave), both serialize on `lm_bind_lock`, no nested-loop `RuntimeError`.
6. **Keystone bidirectional audit test:** assert the emitters flip suppressed→firing on the main path *as intended*, and assert the non-empty-gated readers (`lm_activity.py:90/207/288`) still attribute correctly now that `active_session_id()` is live for the whole turn.

**Two-app real-MCP-tool integration test (the headline):** test #1 above, using `Client(server)` against a real FS/shell MCP tool (not a mock), on the orchestrator path *and* the nanoagent path. Plus a **cross-app soak** under the existing pytest-xdist flake-hunt job (`@pytest.mark.concurrency`, ~50 interleaved iters): two apps, concurrent multi-turn conversations *with delegation and nanoagent fan-out*; assert zero cross-app bleed of hooks, caches, tool telemetry, and provider env.

---

## 7. Risks & open decisions for the owner

**Ranked risks:**
1. **`copy_context` rail is the load-bearing invariant.** After Step 1 it covers nanoagent too, but any *future* DSPy path that runs a tool on a non-context-seeded thread reintroduces the app-less hole. Mitigation: the app-less resolve is now reason-logged, so such a regression is *observable* in the trace rather than a silent gate drop — but it is still a downgrade. Pin with the concurrency test; treat a `tool_runtime_appless_fallback` spike in production traces as a defect signal.
2. **Children-cache app-less collapse** (Site 2) — the one item requiring live-trace validation, not assumption. If a trace shows an orchestrator whose only build is app-less, `app` **must** be threaded in explicitly; do not ship the collapse-to-`finish` on faith.
3. **Deleting `set_global_*` is an API change** — contained to gact + 5 test files (grep-confirmed no CLI/`agent.py` caller). observer/interceptor/cancellation have no instance param, so a non-gact caller wanting them must go through an app or the retained fallback — acceptable, noted.
4. **Per-invocation hook thread unbounded** under a hook storm (semaphore mitigation available); strictly better than deterministic pool exhaustion.
5. **LM-bind lock must stay non-reentrant/tight** to avoid a re-entrant `_apply_lm_provider` deadlock; heavy sub-steps must stay in their own `run_in_executor`.

**Open decisions the owner must make:**
- **(A) One retained fallback bundle vs fail-closed.** This design keeps a single reason-logged `_FALLBACK_TOOL_RUNTIME` for out-of-band/app-less tool calls (preserves #812's deliberate net, satisfies no-silent-fallback). The alternative is **fail-closed**: an app-less tool call with no resolvable gate is *denied* with a structured reason. Fail-closed is stricter (no ungated execution ever) but breaks any legitimate out-of-band caller that relied on the last-installed gate. Recommendation: ship the reason-logged fallback now; revisit fail-closed once traces show whether any real out-of-band tool call exists. **Owner call.**
- **(B) Whether to thread `app` into `_blueprint_runtime_signature` for *all* callers now, or only those the trace proves app-less.** Threading it everywhere is more work but removes the residual dependence on the in-turn keystone for the children cache. Recommendation: thread it wherever a caller already has `app` in scope (cheap), and let the trace decide the rest. **Owner call.**
- **(C) Sequencing of #813's rail fix relative to the develop merge.** Step 1 is a precondition for the deletion but is independently shippable. Confirm the owner wants it folded into the same `fix/735-tool-hooks-contextvar` branch (recommended — the deletion is unsafe without it) rather than a separate #813 PR.

**Files referenced (read-only audit worktree):** `D:/Libraries/Documents/projects/.audit-wt/735-tool-hooks/src/clio_agent/tools/execution.py`, `.../gact/context.py`, `.../gact/runtime/globals.py`, `.../gact/turn.py`, `.../gact/tool_observer.py`, `.../gact/agents/builders.py`, `.../gact/agents/composition.py`, `.../gact/routes/providers.py`, `.../runtime/hooks.py`, `.../runtime/nanoagent.py`, `.../gact/events.py`, `.../agent.py`, `.../tools/file_policy.py`. New module to create: `.../gact/runtime/app_state.py`.