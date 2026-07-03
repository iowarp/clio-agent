# Per-expert provider + LLM off process-global state — #818 / #815

> **Status:** design approved-in-principle, implementation not yet started.
> Produced by the per-expert-provider-lm workflow (map ×5 areas → propose ×3
> candidates → synthesize). Supersedes issues **#815** (P3: LM bind mutates
> process-global state, guarded by a wrong-scoped per-app lock), **#818** (owner
> directive: every expert owns its provider + LLM, guaranteed even distributed),
> and the reverted commit **48969af** (`app.state.lm_bind_lock`, reverted by
> 7cebc77). These are ONE design, not three patches.
>
> **Owner directive (#818, hard requirement):** LM/provider **and credential**
> selection must resolve **per-expert / per-call** (distributed: on the executing
> node), off process-global state, so N experts on N providers run concurrently
> and provider identity travels with the work item across nodes.

---

## 0. The one-line synthesis

The three candidates converge on the same shape and differ only in naming. This
design merges them:

- **Candidate 3's layering** (a serializable spec → credential resolver → spec
  resolver → named registry) — and its key refinement that **credentials are
  resolved *fresh at `forward()`*** (tokens rotate), while endpoint + handshake
  can be resolved once at module `__init__`.
- **Candidate 1/2's immutable, RCU-swapped profile store** (copy-on-write pointer
  swap; readers see old-or-new whole snapshot, never a torn multi-key state) and
  their **no-silent-fallback discipline** (every degraded/ambient path emits a
  structured `stream_fallback` reason).

Net: an expert's provider identity becomes **serializable DATA** (an `LMSpec`
carrying a *credential-ref*, never an inline secret), resolved into an ephemeral
`LMProviderConfig` at the **already-existing** `dspy.context(lm=create_lm(cfg))`
boundary. Nothing on the hot path mutates `os.environ` or dspy
`main_thread_config`. `PUT /v1/providers/lm` is demoted from "install THE process
LM" to "atomically swap the default entry in a per-app profile store." One default
profile === today's single-default LM, so the change is **additive** (RULE 2).

---

## 1. Why not a lock (the #815 finding, restated)

The reverted `app.state.lm_bind_lock` (48969af) is the wrong shape for three
independent reasons, all confirmed in the map:

1. **Scope mismatch (the actual P3 bug).** The lock lives on `app.state` (per
   FastAPI app) but the state it guards — `os.environ` + dspy `main_thread_config`
   — is **process-global**. `build_app` can run twice in one process (the two-app
   test topology); two apps take two different `Lock` objects and still interleave
   into mixed env/dspy state. The lock does not even make the single global bind
   safe.
2. **Serialization is the opposite of the requirement.** A lock serializes access
   to ONE shared global identity. #818 needs N concurrent per-expert identities.
3. **Distributed-meaningless.** A same-process `asyncio.Lock` coordinates nothing
   on another node; process-global env/dspy config is meaningless across nodes.

The correct fix is to **eliminate the shared mutable global**, not lock it. Once
per-expert identity is per-call resolved data, there is no critical section left
to serialize — the lock becomes unnecessary *by construction*, and its scope bug
evaporates because the state it guarded no longer exists as shared mutable global.

---

## 2. What already works (do not re-architect)

The per-call execution boundary is **already** the right, concurrency-safe
primitive and stays byte-for-byte:

- Every expert `forward()` wraps its call in
  `with dspy.context(lm=create_lm(self.config), adapter=create_chat_adapter(self.config))`
  — prompt-only `builders.py:208`, blueprint react `builders.py:1711` (+ schema-
  repair re-extract `:1763`), tool-user `builders.py:2003`.
- The main agent binds `with dspy.context(lm=self._main_lm|self._planner_lm, adapter=self._dspy_adapter)`
  — `agent.py:924 / 1267 / 2121`.
- `dspy.context` sets a **ContextVar** scoped per asyncio task / per thread, so
  concurrent experts never share an LM object.
- `create_lm(cfg)` (`config.py:1161`) builds a self-contained `dspy.LM` from the
  config's `api_base/api_key/model/params` **as constructor kwargs** — it does
  **not** read `os.environ` for those.

The gap is **what feeds `cfg`**, not the mechanism:

- `_dynamic_agent_lm_config` (`builders.py:83`) only carries `api_key`/`api_base`
  forward when the expert's provider **equals** the base provider
  (`builders.py:98-106`); a cross-provider expert gets `api_key=""`,
  `api_base=""` — it cannot authenticate a second provider.
- Handshake (`context_window`, context-aware `max_tokens`, reasoning/tool flags)
  is folded only at the global bind (`providers.py:944`) and propagated to an
  expert **only when provider AND model match base** (`builders.py:123-127`) — a
  distinct expert model runs with `context_window=None` (missing auto-compaction
  denominator) and the static `PROVIDER_DEFAULTS` cap.
- The single provider identity is encoded in process-global `os.environ`
  (`CLIO_LM_*`, stamped `providers.py:718-729`) and dspy
  `main_thread_config['lm']` (`providers.py:971-972`) — a concurrent flip mutates
  both for ALL in-flight experts.

---

## 3. Core objects (owner modules)

### 3.1 `LMSpec` — serializable provider identity (NEW `providers/lm_spec.py`)

A frozen dataclass that *fully* names an LM, safe to persist and to ship with a
work item to another node:

```
LMSpec(
    provider: str,              # "argonne", "openai", "lm_studio", ...
    model: str,
    api_base: str = "",
    credential_ref: str = "",   # e.g. "argonne:default", "openai:acctB"
                                #   — a KEY, NEVER an inline secret
    transport: str = "",        # codex/claude_code transport ("exec"/"sdk")
    temperature: float | None = None,
    max_tokens: int | None = None,
    thinking_budget: int | None = None,
    top_p / top_k / min_p / presence_penalty: ... = None,
)
```

`credential_ref` is a **reference**, never a secret: secrets never serialize into
a stored `AgentDef`, a blueprint frontmatter (packs are checked in), or a trace.
An empty spec === "inherit the default profile," which is today's behaviour.

### 3.2 `CredentialResolver` — keyed, read-only credential resolution (NEW `providers/credentials.py`)

`resolve(provider, credential_ref) -> str` (the api_key), computed **fresh per
call** and **returned** — it NEVER writes `os.environ`. Absorbs the credential
logic currently inside `LMProviderConfig.__post_init__` (`config.py:287-297`) and
`_resolve_argonne_api_key` (`config.py:421`):

- **Cloud default ref** → read the well-known env var `_CLOUD_API_KEY_ENV[provider]`
  (read-only, exactly as today) — preserves baseline for a GACT booted from
  `CLIO_LM_API_KEY` / a provider key env var.
- **Named ref** (`provider:account`) → a per-account keyed source, so two experts
  on the same provider / different accounts each get their own key — the case
  that is impossible today.
- **Argonne ref** → mint/look up a Globus token keyed per ref (reuse
  `argonne_auth.get_access_token`, already env-write-free), lazy and
  non-interactive from constructors.
- **Missing ref on a node** → return `""` and let the LM call surface an
  actionable structured auth error (no silent fallback to the default provider).

### 3.3 `LMResolver.resolve(spec)` — pure spec → config (NEW `providers/resolver.py`)

A pure, idempotent function `resolve(spec, *, cred_resolver, handshake_cache) -> LMProviderConfig`:

1. Fill `PROVIDER_DEFAULTS` (endpoint/model/caps) exactly as
   `LMProviderConfig.__post_init__` does today, but **without** reading
   `os.environ` for the credential.
2. Resolve the api_key via `CredentialResolver.resolve(provider, credential_ref)`.
3. Fold a per-`(provider, model, api_base)` handshake via the EXISTING TTL cache
   (`providers/handshake/cache.py::cached_or_run`, already keyed on
   `(provider_id, api_base)` and never-raises) and `cfg.apply_handshake`, so
   cross-provider experts get correct `context_window` / context-aware
   `max_tokens` / reasoning + tool flags — **closing the `builders.py:123-127`
   gap.** A failed/absent handshake falls back to static `PROVIDER_DEFAULTS` caps
   **with a structured `stream_fallback` reason** (no silent degradation).
4. Return a fully-populated `LMProviderConfig`. The api_key lives only in that
   ephemeral object for the lifetime of one `create_lm` / `dspy.context`.

**Credential freshness (Candidate 3's refinement):** the resolver splits into a
cheap **endpoint + handshake** resolution (safe to compute once at module
`__init__`, cache-backed) and a **`materialize(cred_resolver)`** step that
resolves the api_key fresh at `forward()` — because tokens rotate mid-session.

### 3.4 `ProviderProfileStore` — immutable, per-app registry (NEW `gact/providers/profile_store.py`)

`app.state.provider_profiles` holds an **immutable snapshot** mapping profile-id →
`LMSpec`, with one `"default"` entry. Replaced only by whole-object **RCU pointer
swap** (copy-on-write) — never mutated in place. Per-app (per FastAPI instance),
so the two-app test topology gets two independent defaults instead of racing one
`os.environ`. This is what `_apply_lm_provider`'s write side becomes: a thin
writer, not a world-mutator.

---

## 4. Per-context flow (no global mutation)

```
expert module __init__:
    spec  = build_spec(agent_def, default_profile)         # data, no secrets
    part  = LMResolver.resolve_endpoint_and_handshake(spec) # cache-backed, no key

expert forward():
    cfg = part.materialize(cred_resolver)                   # api_key fresh here
    with dspy.context(lm=create_lm(cfg),                    # UNCHANGED boundary
                      adapter=create_chat_adapter(cfg)):
        ...
```

`_dynamic_agent_lm_config` (`builders.py:83`) is rewritten to build an `LMSpec`
from the `AgentDef` alone (falling back to the default profile's spec when a field
is undeclared) and delegate to `LMResolver` — dropping the `same_provider`
credential/handshake gate entirely. The `dspy.context` call sites do not change.

---

## 5. The admin bind, demoted (`_apply_lm_provider`, `providers.py:677`)

`PUT /v1/providers/lm` becomes an **admin/default** action only:

1. Build an `LMSpec` from the request, resolve it to a config (same resolver
   path), run its handshake (already cached).
2. **Atomic default swap:** `app.state.provider_profiles = snapshot.with_default(spec)`
   — a single pointer assignment (atomic under the GIL).
3. Rebuild ONLY the singleton main agent's LMs (`existing._provider_config /
   _main_lm / _planner_lm / _router_lm / _dspy_adapter`) — the main loop's
   per-context selection, unchanged.

**Deleted from the write path:** `_stamp_process_env` (`providers.py:717-729`,
`1002`, `1040`), the `main_thread_config['lm'|'adapter']` writes
(`providers.py:971-974`), and the whole snapshot→mutate-env→reconfigure-dspy→
restore critical section (`_restore_process_env` / `_restore_dspy_settings`). With
the shared mutable global gone, there is nothing left to serialize — concurrent
default-provider PUTs each build a complete config and do a last-writer-wins
atomic swap (correct "set the default" semantics), and per-expert binds touch
independent registry entries so they never contend.

**Read side:** `_effective_lm_config` (`gact/providers/config.py:55`) and
`_lm_provider_info` are reframed to report the **default profile + per-expert
overrides** from the store, rather than one global `lm_config`.

---

## 6. The boot default stays (baseline safety, RULE 2)

The process-global dspy default is kept as a **harmless boot-time fallback only**:

- Boot (`app.py:1301-1316`) and `setup_dspy` (`config.py:2132`) still call
  `dspy.configure(create_lm(default_cfg))` **once** from the default profile, so
  any un-wrapped ambient caller keeps a valid LM. It is **never re-written** on a
  per-expert path, and experts never rely on it.
- `ClioAgent` still builds `_main_lm / _planner_lm / _dspy_adapter` from the
  default profile — `agent.py`'s `dspy.context` call sites are byte-for-byte the
  same.
- `LMProviderConfig.__post_init__`'s credential env-read (`config.py:293`) is
  **confined to the boot/default config**; the resolver supplies `api_key`
  explicitly on the expert path, so that fallback is never reached for experts. A
  GACT booted purely from `CLIO_LM_*` still authenticates.
- New `AgentDef` fields default empty → the undeclared case resolves to the exact
  config `_dynamic_agent_lm_config` produces today (golden test).

### Ambient call-site sweep (no-silent-fallback rule)

Call sites that today lean on ambient `dspy.settings.lm` must be wrapped in the
active profile's `dspy.context` or take an explicit LM, each emitting a structured
reason if the ambient default is used: `_summarize_segments_llm`
(`gact/agents/runtime.py:53`, auto-compaction), `gact/runtime/context_tokens.py`,
`usage.py`, `app.py::_model_of_lm`, `gact/runtime/globals.py`. A miss becomes
queryable, not invisible.

---

## 7. Concurrency (safe by construction)

1. Each expert's resolved `LMProviderConfig` and its `dspy.LM` are **ephemeral**,
   created inside one `forward()` and entered via ContextVar-scoped
   `dspy.context`. Two concurrent experts never share an LM object or credentials.
2. The only cross-call shared state is **read-mostly + immutable**: the
   `ProviderProfileStore` is a snapshot replaced by atomic pointer assignment —
   readers see old-or-new whole snapshot, never a half-written multi-key mix
   (unlike today's `os.environ` + `main_thread_config` interleave).
3. The handshake cache (`cached_or_run`) already dedupes in-flight runs and is
   idempotent — a racing double-run recomputes the same discovery.
4. `CredentialResolver` only READS env/token sources — N concurrent experts on N
   providers resolve independently with zero contention.
5. N experts on N providers run **truly in parallel** (the #818 requirement),
   which a serializing lock could never deliver.

**Residual hazard — `claude_code` `_SDK_SESSION`.** It is a process-wide singleton
keyed on `(model, cwd)` that serializes/thrashes if two experts want different
`claude_code` models concurrently. This design makes concurrent distinct
`claude_code` experts *expressible*, so the singleton MUST become a keyed session
pool. Tracked as a required follow-on step (§9 step 10), not deferred.

---

## 8. What this subsumes

- **#818** — DELIVERED: each expert carries a full `LMSpec`
  (provider + model + endpoint + credential_ref + params) as serializable data;
  resolution happens at the executing boundary against node-local
  credential/handshake sources, so identity travels with the work item and N
  experts on N providers run concurrently.
- **#815 (P3)** — the process-global mutation the lock tried to protect
  (`_stamp_process_env` + `main_thread_config['lm']`) is ELIMINATED from every
  per-expert path and reduced on the default path to an atomic immutable-snapshot
  swap. No critical section remains; the scope mismatch evaporates. #815's narrow
  single-LM admin-bind fix is absorbed as the default-profile swap + main-agent
  instance rebuild.
- **48969af (reverted lock)** — not reinstated and provably unnecessary: the state
  it guarded no longer exists as shared mutable global.

---

## 9. Ordered implementation steps

Each step keeps `uv run pytest tests/ -m "not integration"` green and preserves
the baseline; the final step flips per-expert resolution on by default.

1. **`CredentialResolver` (extract, additive).** New `providers/credentials.py`
   with `resolve(provider, credential_ref) -> str`. Move the cloud-env lookup
   (`config.py:287-297`) and `_resolve_argonne_api_key` (`config.py:421`) logic
   behind it; have `LMProviderConfig.__post_init__` delegate to it for the default
   ref so behaviour is identical. *Tests:* default ref reads the same env var;
   argonne ref returns the override token; a named ref reads a distinct source;
   unknown/missing ref returns `""`; `__post_init__` baseline unchanged.

2. **`LMSpec` (data, additive).** New `providers/lm_spec.py`: frozen dataclass +
   `spec_from_config(cfg) -> LMSpec` and `build_spec(agent_def, default_spec)`.
   Nothing consumes it yet. *Tests:* round-trips through serialize/deserialize;
   carries no secret field; `build_spec` inherits default fields when the
   `AgentDef` declares none.

3. **`LMResolver` (pure, additive).** New `providers/resolver.py`:
   `resolve_endpoint_and_handshake(spec)` (PROVIDER_DEFAULTS fill + cached
   handshake fold, no key) and `ResolvedLMSpec.materialize(cred_resolver) ->
   LMProviderConfig`. Handshake via `handshake/cache.py::cached_or_run`; on
   failure fall back to static caps with a structured `stream_fallback` reason.
   *Tests:* golden equivalence — for a same-provider spec the output equals
   today's `_dynamic_agent_lm_config`; a cross-provider spec gets a real key +
   folded `context_window`/`max_tokens`; handshake failure emits the reason.

4. **`ProviderProfileStore` (additive, unread).** New
   `gact/providers/profile_store.py`: immutable snapshot `{id -> LMSpec}` with a
   `"default"`, RCU `with_default`/`with_profile`. Seed the default from
   `load_config_from_env()` at boot (`app.py:_build`). Nothing routes through it
   yet (shadow). *Tests:* `with_default` returns a new snapshot (old unchanged);
   pointer swap is atomic; two `build_app` instances hold independent stores.

5. **Schema fields (additive, defaults empty).** Extend `AgentDef`
   (`gact/types.py:786`) with explicit `api_base`, `credential_ref`, `transport`
   (data, never inline secrets). Forward them in `parse_expert_file`
   (`gact/expert_packs.py:295`) and add them to the overlay patchable set
   (`gact/agents/resolution.py`). *Tests:* frontmatter `credential_ref:`/`api_base:`
   parse into `AgentDef`; a session overlay can patch them; absent fields default
   `""`.

6. **Rewrite `_dynamic_agent_lm_config` (behaviour-preserving swap).**
   `builders.py:83` becomes a thin delegate: `build_spec(agent_def,
   default_profile.default)` → `LMResolver.resolve_endpoint_and_handshake` →
   store the `ResolvedLMSpec` on the module; `forward()` calls `materialize` and
   feeds the unchanged `dspy.context`. Drop the `same_provider` gate
   (`builders.py:98-127`). *Tests:* **per-expert selection** — a cross-provider
   expert authenticates and gets its own handshake; **backward-compat** — an
   undeclared expert resolves byte-identical to the pre-change config (golden);
   **concurrency** — N experts on N providers run concurrently, each `dspy.LM`
   carries its own model/api_base/api_key with no cross-talk.

7. **Ambient call-site sweep.** Wrap `_summarize_segments_llm`
   (`runtime.py:53`), `context_tokens.py`, `usage.py`, `app.py::_model_of_lm`,
   `runtime/globals.py` in the active profile's `dspy.context` or pass an explicit
   LM; emit a structured `stream_fallback` reason on ambient use. *Tests:* each
   runs under a bound context; a forced ambient path records the reason.

8. **Demote `_apply_lm_provider` (default-only).** `providers.py:677`: build spec
   → run handshake → **atomic default swap** into the store → rebuild the
   singleton agent's `_main_lm/_planner_lm/_router_lm/_dspy_adapter`. **Remove**
   `_stamp_process_env` (718-729/1002/1040), `main_thread_config['lm'|'adapter']`
   writes (971-974), and the env/dspy snapshot-restore closures. Reframe the read
   side (`_effective_lm_config`, `_lm_provider_info`) to report default +
   overrides. *Tests:* **concurrency-safety** — two concurrent PUTs for different
   providers yield ONE internally-consistent default snapshot (not a mixed env
   state) [replaces the reverted-lock's failing-first test]; **backward-compat** —
   single-provider bind + `GET /v1/providers/lm` + `/wait` still report `ready`;
   CLI smoke (`uv run src/clio_agent/ui/cli.py`) unaffected.

9. **Flip on by default.** Make the `ProviderProfileStore` the authoritative
   source: `ClioAgent` and experts resolve off the store's default (not
   `load_config_from_env()` inheritance / `base_agent._provider_config`), and drop
   the boot env-handoff (the `dspy.configure` default remains a harmless
   fallback). This is where per-expert resolution is on for every path. *Tests:*
   full baseline suite green; a two-expert-two-provider end-to-end turn; CLI smoke
   + GACT single-provider operation unchanged.

10. **Follow-on (required for the concurrency guarantee).** Convert
    `claude_code` `_SDK_SESSION` (`providers/claude_code_litellm.py`) to a keyed
    session pool (per `model`+`cwd`); drop the `os.environ` transport fallbacks
    (`claude_code_litellm.py:884/1073`, `codex_litellm.py:385`) so transport is
    purely per-LM `optional_params`. *Tests:* two concurrent distinct
    `claude_code` experts each hold their own session; transport read comes only
    from the per-LM config.

---

## 10. Risks

1. **Cold handshake latency** on a cross-provider expert's first `forward()` —
   mitigated by the `(provider_id, api_base)` TTL cache (`cached_or_run` dedupes),
   pre-warm on profile registration, and a never-raise fallback to static caps
   **with a structured reason**.
2. **Missed ambient consumer** silently using the boot default — mitigated by
   keeping a real boot default (baseline) AND the §9 step 7 sweep, with a
   structured reason on every ambient use so a miss is queryable, not invisible.
3. **`credential_ref` is new security surface** — enforce ref-only (reject inline
   secrets in `AgentDef` persistence); secrets resolve at runtime and never
   serialize into stored defs or traces.
4. **`claude_code` `_SDK_SESSION` thrash** under concurrent distinct models — the
   keyed pool (step 10) is required, not optional, for the guarantee.
5. **Last-writer-wins default under concurrent PUTs** is correct "set the default"
   semantics but must return the resolved winning state so the client isn't
   confused — surfaced structurally, never silent.
6. **Distributed credential resolution** assumes each node's `CredentialResolver`
   can resolve a given ref; a missing ref on a node must surface an actionable
   structured auth error at resolve time, not a silent fallback to the default
   provider. The node-side secret transport itself is out of scope here — the
   `credential_ref` (not inline key) decision keeps it open.
