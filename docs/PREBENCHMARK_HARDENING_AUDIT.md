# CLIO Pre-Benchmark Hardening Audit

Updated: 2026-06-02

This audit is the final backend readiness pass before the 1.0 benchmark
campaign. It separates three states that are easy to confuse:

- **implemented**: CLIO has code and API/runtime support.
- **tested**: the configured test suite covers the behavior.
- **benchmark-ready**: the behavior has real-provider evidence, preferably from
  the ALCF/Argonne lane, that is strong enough for scientific review.

The current conclusion is that CLIO `develop` is clean enough to begin the
final benchmark campaign, but several runtime-semantic surfaces remain
**benchmark-proof pending**. They should not be described as fully 1.0-proven
until the benchmark produces structured session evidence for them.

## Current Baseline

- CLIO `origin/develop`: `3fa1622`
- Marketplace repo inspected: `/home/jcernuda/clio-agent-marketplace` at
  `e145977`
- Open CLIO PRs: none at audit time
- Existing open benchmark/deferred issues:
  - #441 asynchronous child delegation, deferred design
  - #510 marketplace hierarchy depth and coverage
  - #511 per-expert provider/model/profile benchmark proof
  - #514 marketplace commands and agent-invocable command benchmark proof
  - #515 workspace-local/global Agent Blueprint and memory benchmark proof
  - #516 semantic regression benchmark gate
  - #518 deterministic shortcut audit
  - #528 true multimodal image request support, deferred
  - #540 Agent Blueprint packaged hooks and trust semantics

Configured backend gates remain:

```bash
uv run ruff check src/ tests/ scripts/create_demo_data.py
uv run mypy src/
uv run pytest tests/ -m "not integration" --cov-fail-under=70
```

## Audit Matrix

| Surface | Current support | Existing evidence | Gap / risk | Status |
| --- | --- | --- | --- | --- |
| Agent Blueprint v1 contract | `AGENT.md` plus `experts/*.md`, strict v1 validation, built-in and installed Blueprint discovery, session activation, validation diagnostics. | `docs/AGENT_BLUEPRINT_V1_CONTRACT.md`; `tests/test_gact/test_agent_blueprints.py`; current marketplace report proves installed packs can run. | Real-provider evidence is concentrated in marketplace lane; future packs still need validation gates before being called 1.0-ready. | Implemented and tested; benchmark-proof pending per #510/#516. |
| Marketplace pack completeness | Packs can package experts, prompt bodies/ids, skills refs, commands refs, built-in tool refs, MCP descriptors, prompt profiles, provider/model defaults. | Marketplace files plus tests for parser/runtime surfaces. `remote-sensing-ogc-review` contains one MCP descriptor; no current marketplace pack packages commands or hooks. | Current marketplace packs are mostly shallow except seismic; command and provider/model/profile semantics are not proven in marketplace real-provider evidence. | Open benchmark gaps #510, #511, #514. |
| Expert-declared skills | Expert frontmatter supports `skills`; runtime resolves pack-local, workspace-local, and global skill bodies into expert prompts; provenance records resolution. | `docs/AGENT_BLUEPRINT_V1_CONTRACT.md`; `tests/test_gact/test_agent_blueprints.py`; `tests/test_gact/test_prompts_api.py`. | Real-provider benchmark must prove skill body influence from a marketplace pack, not only parser/runtime injection. | Implemented/tested; benchmark-proof pending #516. |
| Per-expert provider/model/profile | Expert fields exist and runtime provenance records provider/model/profile source. | Parser/runtime tests; provenance contract. | Current marketplace evidence mostly uses global provider/model fallback. Distinct per-expert runtime choices need a real-provider benchmark. | Open #511. |
| MCP descriptors in Agent Blueprints | `tools/*.md` descriptors support transport, install/runtime metadata, explicit trust, enablement, probing, and runtime tool exposure. | `docs/AGENT_BLUEPRINT_V1_CONTRACT.md`; `tests/test_gact/test_agent_blueprints.py`; MCP reconnect tests. | Only one marketplace pack currently carries an MCP descriptor; real-provider evidence must prove enablement and tool use from a marketplace pack. | Implemented/tested; benchmark-proof pending #516. |
| Hooks | Runtime hooks support local Python handlers, backend factory/disable mode, global and scoped directories, timeouts, fail-closed pre hooks, fail-open post/semantic hooks, capability metadata. | `docs/SEMANTIC_EXECUTION_TRACES.md`; `tests/test_gact/test_hooks.py`; `tests/test_gact/test_semantic_events.py`. | Hooks are not yet pack-installable from Agent Blueprints/marketplace with explicit trust. Hook coverage is unit/API-level, not ALCF real-provider proof. | Runtime implemented/tested; packaged hooks open #540. |
| Semantic execution logging | Semantic events support file JSONL and factory backends, redaction levels, live SSE, hook dispatch, and report ingestion. | `docs/SEMANTIC_EXECUTION_TRACES.md`; `tests/test_gact/test_semantic_events.py`; benchmark report code. | Need ALCF real-provider trace review for depth: LLM request/response, delegation, tool/MCP, hook, memory, command, artifact, error, and recovery evidence in one run family. | Implemented/tested; real-provider proof pending #516. |
| Runtime provenance | Assistant metadata records `metadata.runtime_provenance` with turn, workspace, agent, blueprint, provider, prompt, tools, commands, skills, delegation, memory, context, artifacts, errors. | `docs/RUNTIME_PROVENANCE_CONTRACT.md`; `tests/test_gact/test_expert_packs.py`; `turn.completed` includes assistant metadata. | `commands.observed` and artifacts are intentionally conservative summaries; benchmark proof should combine provenance with semantic events instead of relying on final text. | Implemented/tested; benchmark-proof pending #516. |
| Live observability / streaming | Semantic events are emitted before final `message.completed`; tool events and delegation events are exposed independently of provider token streaming. | `tests/test_gact/test_semantic_events.py`; `tests/test_gact/test_streaming.py`; closed #531. | Need ALCF real-provider temporal proof that long turns show useful intermediate state, not only synthetic/fake-agent tests. | Implemented/tested; real-provider proof pending #516. |
| Workspace scope | Workspaces have root/storage roots, workspace-local session mirroring, default workspace behavior, workspace-filtered sessions, and workspace-local pack/command/prompt surfaces. | `docs/WORKSPACE_SCOPE_RUNTIME_V1.md`; workspace/session/context tests. | Combined local-vs-global proof with two workspaces and overlapping resources is not yet in the marketplace benchmark. | Open #515. |
| Memory tools and compartmentalization | Memory search/read tools enforce current-session defaults, same-workspace user intent, other-workspace denial, and explicit global scope. | `docs/AGENT_MEMORY_TOOLS_V2.md`; `tests/test_gact/test_memory_stats.py`. | Needs real-provider proof that an agent actually invokes/uses this capability for “based on recent work” semantics without leaking other workspaces. | Implemented/tested; benchmark-proof pending #515/#516. |
| Commands and slash-command truth | Backend commands, workspace command files, compatible Claude command files, user-agent commands, agent-invocable command allowlists, disabled/unavailable commands, and `/cache-stats` failure behavior are tested. | `docs/COMMAND_CAPABILITY_AUDIT.md`; `tests/test_gact/test_commands.py`. | Marketplace-packaged command behavior is not proven by a real marketplace benchmark. | Open #514. |
| Permissions | Permission requests, policy persistence, destructive gate behavior, deny/allow audit, MCP tool policy checks, and direct destructive route checks are covered. | `docs/PERMISSIONS.md`; `docs/PERMISSION_POLICY_PERSISTENCE.md`; permission tests. | Needs benchmark trace proof for permission decisions if any final 1.0 benchmark case exercises gated writes/tools. | Implemented/tested; benchmark-proof conditional #516. |
| Undo/rewind/cancel | Visible transcript rollback, permission-audited destructive mutation, running-session rejection, and cancellation/error surfacing are tested. | `docs/UNDO_REWIND_DESIGN.md`; session rollback/cancellation tests. | Durable ARC tombstone semantics are not claimed; rollback scope remains visible transcript. | Implemented/tested for claimed scope. |
| Context files and attachments | Workspace-relative context files, content preview endpoint, attachment upload, confinement, large/invalid file handling, and message injection are tested. | Context file/content/attachment tests; file mention docs. | True multimodal image ingestion is deferred; uploaded images are stored/previewable but not model-visible as image parts. | Text/data ready; vision deferred #528. |
| Provider configuration and swaps | Provider catalog/configuration, ALCF token status, LM Studio/Ollama/OpenAI-compatible paths, async provider swap for blocking providers, and provider provenance are tested. | Provider tests; benchmark runner supports provider-swap case. | Provider-swap stress remains future benchmark coverage; ALCF readiness depends on environment token status during benchmark. | Implemented/tested; stress proof pending #516. |
| Deterministic shortcut removal | Keyword user-agent routing is opt-in; benchmark lanes reject shortcut route sources and require root-owned sync delegation evidence. | `tests/test_gact/test_post_messages.py`; `tests/test_scripts/test_run_demo_benchmark.py`; benchmark reports. | A deliberate audit of remaining shortcut/control-flow paths is still open. | Open #518. |
| Marketplace benchmark evidence | Current `MARKETPLACE_UNIFIED_REPORT.md` proves five marketplace Blueprints, root-owned sync delegation, one complex seismic chain, and one verified PNG artifact. | `benchmark/CURRENT_STATUS.md`; `benchmark/MARKETPLACE_UNIFIED_REPORT.md`. | Current evidence has only one complex pack; stress coverage gaps remain by design. | Valid current smoke/hierarchy proof; expansion open #510/#516. |

## Critical Findings

1. **Packaged hooks are not implemented.** Runtime hooks are real, but
   marketplace/Agent Blueprint packaged hooks need a trust/install design before
   CLIO can claim fully self-contained hook-capable packs. Tracked as #540.

2. **Runtime semantics need ALCF proof before 1.0-ready language.** Many
   surfaces are unit/API-tested, and some have Codex marketplace evidence. The
   final benchmark must provide ALCF/Argonne real-provider proof for the
   runtime-semantic claims: hierarchy, parent resume, live events, traces,
   memory access, tool/MCP calls, artifacts, and recovery.

3. **Marketplace coverage is intentionally not broad enough yet.** The seismic
   pack is meaningful and complex. Most other packs remain shallow smoke/domain
   tool proofs. That is acceptable as current evidence only if release language
   does not overstate it.

4. **Observability exists, but scientific adequacy must be reviewed from logs.**
   The trace/logging system can emit the right classes of events, but the final
   benchmark review must inspect actual JSONL/session logs and verify that the
   depth is sufficient for reconstruction without final-answer string guessing.

5. **No new backend correctness blocker was found during this pass.** The gaps
   identified here are either benchmark-proof gaps, deferred feature scope, or
   the newly tracked packaged-hooks feature. If the ALCF benchmark exposes a
   runtime contradiction, it should become a dedicated blocker issue and PR.

## Pre-Benchmark Acceptance Checklist

Before declaring the benchmark campaign ready to cite for 1.0:

- Run configured CLIO gates on `develop`.
- Confirm no open CLIO PRs.
- Confirm marketplace repo is clean and pushed.
- Run the ALCF/Argonne real-provider benchmark lane with semantic tracing
  enabled at least at `semantic` detail:

```bash
CLIO_SEMANTIC_TRACE_BACKEND=file \
CLIO_SEMANTIC_TRACE_DETAIL=semantic \
uv run python scripts/run_demo_benchmark.py \
  --require-lane-criteria \
  --lane marketplace_agents \
  --base-url http://127.0.0.1:17960 \
  --data-dir tmp/clio-benchmark-data \
  --output-jsonl benchmark/ALCF_MARKETPLACE_AGENT_EVIDENCE.jsonl \
  --report benchmark/ALCF_MARKETPLACE_AGENT_REPORT.md
```

- Inspect JSONL proof, not only Markdown reports. At minimum verify:
  - active Agent Blueprint id/scope/path,
  - selected root expert and child delegation rows,
  - `delegation.completed` and `delegation.parent_resumed`,
  - provider/model/prompt/profile provenance,
  - declared versus observed tools,
  - MCP descriptor/install/trust provenance where relevant,
  - memory policy decisions when memory tools are used,
  - hook events if hooks are configured,
  - artifact existence and producer evidence,
  - structured errors/recovery decisions.

## Release Language Guardrail

Until the ALCF benchmark evidence exists, use this wording:

> CLIO `develop` has backend support and configured test coverage for Agent
> Blueprints, expert skills, MCP descriptors, hooks, semantic traces, runtime
> provenance, workspace/memory scoping, commands, permissions, rewind, and
> attachments. Final 1.0 scientific readiness remains pending the ALCF
> real-provider benchmark evidence and the open benchmark-hardening issues.

Do not say:

> All marketplace, hook, memory, and observability semantics are fully proven
> end to end.

