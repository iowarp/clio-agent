# Campaign: Land the A2UI/v0.3 Rework + Flowcept/CMF on develop

**Status:** ACTIVE (approved 2026-08-25). Owner-approved plan; every owner decision is resolved in this document — the executor never has to ask.
**Executor:** Codex. **Reviewer:** Claude (this repo's review session). **Flowcept:** owned by a separate session; the owner coordinates its develop merge directly.
**Keep this doc updated as work lands** (house rule; cf. `system-cleanup-2026-07.md`).

## Context

Codex produced four branches (clio-agent `codex/gact-a2ui-v091-producer` @ fc4ea160, gact-tui `codex/gact-tui-node-revamp` @ 2a425f12, marketplace `codex/earthscope-inherit-session-model` @ 8c0e4a2, clio-kit `codex/mcp-launcher-nonblocking-budget` @ d301bf7). An eight-agent review (compliance + five design angles) reached this verdict: **genuinely good architecture, ungoverned** — zero CI ever ran, both large branches fail their own gates, several claims are false at HEAD, and the recurring defect signature is "correct pattern applied once, not carried to the sibling."

Separately, `feat/flowcept-provenance` (14 commits, live-qualified, Flowcept/CMF provenance + blueprint-MCP fixes) was never merged and the Codex rework never saw it. Both branches rewrote the **same blueprint-discovery method** in `agent.py`; git auto-merges them into code neither branch ever ran.

**Goal:** fix every review finding (owner decision: ALL of them pre-merge, including the quarter-tier architecture items), absorb flowcept/CMF, and end with all four repos merged to develop (main for marketplace/clio-kit per their flows) through PRs with **fully green CI — every test passes; failed or skipped-that-should-run counts as failure**. Stop at develop; no release cut.

**Executor:** Codex, which has the full UI development stack — project browser control, the gact-tui skill pack (`external/gact-tui/.claude/skills`: load `gact-change-control`, `gact-architecture-contract`, `gact-wire-protocol-reference`, `gact-validation-and-qa` before any wire/UI change), shadcn registry MCP, reui MCP. This session (Claude) reviews the PRs; the other Claude session owns flowcept and lands it first.

## How this campaign runs

This document is the campaign's source of truth. Codex's first act is Phase 0: file the GitHub issue set (one umbrella per repo + one issue per workstream, each carrying its full spec — file:line, failure scenario, acceptance — copied from this doc; issues are the loop's grounding, never bare bodies) and open the four draft PRs so CI becomes the verification loop. Phase 0 umbrellas: clio-agent [#1249](https://github.com/iowarp/clio-agent/issues/1249), gact-tui [#371](https://github.com/iowarp/gact-tui/issues/371), marketplace [#54](https://github.com/iowarp/clio-agent-marketplace/issues/54), clio-kit [#381](https://github.com/iowarp/clio-kit/issues/381).

**All owner decisions are resolved in this document — there are none left to ask mid-campaign.** (See the final section.)

## Non-negotiable ground rules (violations reopen the PR)

1. **No silent fallback** — every degradation emits a typed reason reaching trace/API (model: `_stream_fallback_reasons` in `gact/streaming.py`). No bare `except: pass`, no `contextlib.suppress(Exception)` on new paths.
2. **No ratchet raises** — `check_file_size.py` / `check_silent_fallbacks.py` baselines go DOWN only. Fix accretion by moving code into owner modules, never by editing baselines.
3. **Tests first for bugfixes** — every bug below lands with a failing-first test; error paths and concurrency, not just happy path.
4. **Server owns semantics** — no new client-side filters/heuristics; deleting one means filing+fixing the server defect it masked.
5. **Two-lane observability** — model lane bounded (lean), trace/UI lane byte-verbatim; **no trace-lane fact may be derived from a model-lane projection**.
6. **Protocol by negotiation, never timing** — no global timeouts over composite work; version/era from handshake only.
7. Do not touch `.tmp/clio-agent-flowcept` (another session's worktree). Conventional commits, no `--no-verify`.

## Phase 0 — Freeze, open draft PRs, file the issue set

1. Stop feature work on all four branches (done — tips above are final inputs).
2. **Open draft PRs immediately** for all four branches against their targets (clio-agent→develop, gact-tui→develop, marketplace→main, clio-kit→main). CI only triggers on PRs/develop/main — this is the verification loop for everything below. Record the red baseline of the first runs.
3. File GitHub issues from this plan: one umbrella per repo + one issue per workstream below, **with the full spec (file:line, failure scenario, acceptance) copied in** — never bare issue bodies. Link all to the umbrella.

### Phase 0 record — 2026-08-26 06:54Z

- **Issue set:** 19 issues filed: four umbrellas plus all 15 workstream issues. The umbrella issues link their workstreams, and every workstream body carries the source file/line anchors, failure scenario, acceptance contract, and binding campaign rules from this document.
- **clio-agent:** draft PR [#1255](https://github.com/iowarp/clio-agent/pull/1255), `codex/gact-a2ui-v091-producer` → `develop`, first observed at `2ea8bf9d`. **RED / unverified:** GitHub created no check suite or `pull_request` workflow run, and the PR is merge-conflicted (`DIRTY` / `CONFLICTING`). Absence of checks is not green.
- **gact-tui:** draft PR [#380](https://github.com/iowarp/gact-tui/pull/380), `codex/gact-tui-node-revamp` → `develop`, first run at `b070eb6c`. **RED:** [workspace/apps](https://github.com/iowarp/gact-tui/actions/runs/32939690319) failed the component-reuse ratchet because `a2ui-catalog.tsx` no longer imports the sourced AI Elements artifact component; [CI](https://github.com/iowarp/gact-tui/actions/runs/32939690213) failed the tracked-media policy (two Playwright snapshots and `web/src/assets/hero.png`) and the dev-install revision check (`dev` versus the PR merge SHA); [docker](https://github.com/iowarp/gact-tui/actions/runs/32939690356) failed `clio-web` on TypeScript errors in `time-series-plot.tsx`. `python-adapter`, `clio-api`, and `clio-tui` passed; the Tauri matrix was skipped after the React gate failed.
- **marketplace:** draft PR [#56](https://github.com/iowarp/clio-agent-marketplace/pull/56), `codex/earthscope-inherit-session-model` → `main`, first observed at `8c0e4a20`. **RED / unverified:** GitHub created no check suite or `pull_request` workflow run. The branch is mergeable, but absence of checks is not green.
- **clio-kit:** draft PR [#383](https://github.com/iowarp/clio-kit/pull/383), `codex/mcp-launcher-nonblocking-budget` → `main`, first run at `d301bf7a`. **RED:** [Quality Control](https://github.com/iowarp/clio-kit/actions/runs/32939688034) and [Publish Python Package](https://github.com/iowarp/clio-kit/actions/runs/32939688370) both failed the file-size ratchet because `src/clio_kit/env_cache.py` is 790 lines against the recorded 770-line baseline. The baseline-diff guard passed; downstream quality and publication jobs were skipped by the failed prerequisite and therefore are not accepted as verified.
- No local test suite was run during Phase 0. This is the immutable first GitHub-run baseline; all failures and missing/skipped checks remain campaign work, and no ratchet baseline may be raised to clear them.

## Phase 1 — Flowcept/CMF absorption (UNBLOCKED — dependency already satisfied)

**Status 2026-08-26:** `feat/flowcept-provenance` is ALREADY on develop (7d1e152f proven an ancestor, zero missing commits; landed via PR #1248 + the edge-close train, CI-gated; the branch is deleted). No waiting — start Phase 1 immediately. Two facts moved since this doc was drafted: the path-activated blueprint resolution now lives in **`gact/blueprint_activation.py`** (refactored out of `agent.py` on develop), and the Flowcept qualification recipe is repo-committed at **`scripts/provenance_qualification/`** (parameterized serve + b=transform(a) driver + qualified homelab env sample, bdae6496).

On `codex/gact-a2ui-v091-producer`:
1. `git merge origin/develop`. Expected: one textual conflict (`docs/ENVIRONMENT.md` — resolve by running `python scripts/gen_env_reference.py` and committing all three generated artifacts, which W1.5 requires anyway).
2. **Reconcile the semantic collision in blueprint discovery**: develop carries the flowcept path-activated session-blueprint resolution (now in `gact/blueprint_activation.py`: explicit session blueprint path → `parse_agent_blueprint_root`, ahead of installed discovery); the Codex branch changed `discover_agent_blueprints(cwd=Path(cwd))` in `agent.py`. The merged flow must: try the explicit session path first (flowcept semantics), fall through to cwd-aware discovery (Codex semantics), and **replace every swallow with a typed structured reason** (see W2.1 — any surviving `except Exception: if verbose: print` hunks from either side get the same treatment).
3. Run the merged `tests/test_core/test_agent_blueprint_lazy_mount.py` (develop gained the flowcept acceptance suite, +117 lines; Codex broke 3 pre-existing tests there) — all green is the acceptance for this phase.
4. Re-verify CMF provenance identity against the reworked artifact custody: the flowcept work stamps CMF execution identifiers via `gact/routes/artifacts.py` + `gact/sessions.py`; Codex changed artifact identity/minting ("preserve child artifact identities", preview minting). Re-run the provenance test suite + one live Flowcept qualification run using the repo-committed recipe at `scripts/provenance_qualification/`.

## Phase 2 — clio-agent backend workstreams

### W1 — CI gates green (no baseline edits)
- **File-size (22 files red):** move the +965 lines added to already-oversized files into the 7 owner modules the branch created; **split `protocol_v3.py` (813 lines) into `gact/protocol/v3/{workspace,session,message,event,capabilities}.py`** and convert its two ~150-line if/elif chains (`transcript_entities`, `event_to_v3`) to lookup tables (the module already uses tables: `_SESSION_STATE`, `_event_identity`).
- **Silent-fallback ratchet:** fix `lm/adapters.py:250-253` (`except Exception: pass`); audit the seven `contextlib.suppress(Exception)` sites in `codex_stream.py` (incl. `turn.interrupt()` at :443) — typed reasons or narrow exception types.
- **mypy (49 errors, 39 in protocol_v3):** fix during the split; also `codex_stream`, `expert_packs`, `builders`, `codex_litellm`, `messages`.
- **Route-count guardrail:** update `EXPECTED_ROUTE_METHOD_PAIRS` 192→203 with intent (or move inline routes into routes modules).
- **Env reference:** regenerate `.env.example` + `docs/ENVIRONMENT.md` + third artifact via `scripts/gen_env_reference.py` (`.env.example` still carries deleted `CLIO_CODEX_STATEFUL_CAPACITY`).
- **pytest:** all 14 branch-caused failures green (individually specced in W2/W3); coverage stays ≥78.

### W2 — Proven bugs (each with failing-first test)
1. **B1** `agent.py:328-331` discovery swallow → typed structured reason on discovery failure (a silently toolless fleet is forbidden); fixes the 3 `TestActiveToolExecutorBlueprintScoping` tests. Merged with Phase 1 step 2.
2. **B2** `gact/a2ui.py:545-557` ledger corruption → typed load-failure reason, quarantine the corrupt file (rename aside), never destructive-overwrite. (Superseded structurally by W4.1 but the no-data-loss test lands here.)
3. **B4** `PATCH /v1/session-defaults` rejects half-filled model refs (422 with reason); defaults reconciled on provider swap; corrupt-file reset emits typed reason (`session_defaults.py:52-59`).
4. **M1** `routes/a2ui.py:78-87` message batch atomic — validate all (including state checks) before applying any.
5. **M2** `deleteSurface` terminal on the HTTP route (`a2ui.py:626-631`), matching the tool path.
6. **ok/is_error derivation** `tools/execution.py:837` — classify from `outcome.raw_result`, not the bounded `model_text` (mirror `gact/agents/builders.py:805-811`). Test: >12k-char structured failure yields `ok: false` on the wire.
7. **A2UI 512-message eviction** `a2ui.py:621-622` — typed eviction envelope with dropped-count (copy `mcp_executor.py:658-682` pattern); must never evict `createSurface` (compaction W4 makes this moot for component revisions, keep the guard).
8. **`test_tool_telemetry` split** — the trace-lane test pins byte-exact `structured_content` (passes today); a separate model-lane test pins the bounded envelope contract, not raw equality. Bound threshold moves to `conf.resolve` + a typed log line + the existing `{preview, truncated, original_chars}` vocabulary, owned by `gact/evidence.py`.
9. **Removed-transport rejection** back to typed domain 400 (not generic Pydantic 422) — `test_lm_provider.py` pin.
10. **Wire-semantics handoff `StopIteration`** ×2 — repair reconciliation after the `emit_spawn_started` rework (46c14345); the 2 deleted cancellation guarantees (`test_cancel_during_turn…`, `test_cancel_before_turn…`) are re-asserted against the new session flow.
11. **New builtin skill** `present-interactive-analysis` added to the expected-skill-list test.

### W3 — Provider layer
1. **Doctor probe** `runtime/lm_provider_probe.py:85-88,142`: codex readiness = `importlib.util.find_spec("openai_codex")` + venv-bundled binary + `auth.json` presence — not PATH.
2. **Reasoning boundaries**: restore `summaryPartAdded` handling in `codex_stream.py` `astream_sdk` dispatch (emit `"\n\n"` between summary parts, per deleted 532b492e) + its test.
3. **Credential reaper**: startup sweep of orphaned `%TEMP%/clio-codex-sdk-*` dirs (and cap live copies); typed log of what was reaped.
4. **`_ALLOWED_ITEM_TYPES`** (`codex_stream.py:77,266-276`): deny by **action-class** (anything that executes/mutates → hard fail; unknown informational item types → typed skip reason), so a benign 0.147.x SDK patch can't kill every turn.
5. **120s composite deadline** (`codex_stream.py:46,385-401`): replace with per-exchange progress deadlines (reset on every SDK event) resolved via `conf.resolve`, documented in ENVIRONMENT.md.
6. **Zero-retry codex branch** `lm/adapters.py:505-513`: emit a typed reason when retries are suppressed; prefer expressing it as a capability on the provider config rather than a name check.
7. **DSPy coupling**: convert `_forced_submit`/`_make_submit_tool` from byte-forks to the lockstep pattern documented at `reactv2.py:112-116` where feasible; at minimum add `hasattr` + signature assertions for every consumed private symbol and a drift test that diffs the forked bodies against the installed `dspy/predict/react_v2.py` (fails loudly on any upstream change).
8. **Lazy imports restored** in `model_discovery/codex.py` (module-scope SDK import currently loads the Codex SDK for every user on every provider).
9. **Stateful-delta removal**: record the decision as a typed, documented tradeoff (docstring + ENVIRONMENT note: SDK ownership + single cancel path in exchange for ~2.5× TTFT; no code revert). Delete `stateful_common.py`'s dead hooks (`extra`, `open_handle` pluggability) or re-justify in-module.

### W4 — A2UI backend architecture (owner decided: pre-merge)
1. **Ledger → projection.** Kill the `a2ui-surfaces.json` sidecar. Surface state becomes derivable from the message log: full A2UI protocol messages ride the existing `Part` machinery (`parts.py:285-287` already carries `type="a2ui"` + `surface_id` — extend it to carry the message payload), and `transcript_entities` **derives** `surfaces` by folding parts like every other entity (delete the `"surfaces": []` placeholder at `protocol_v3.py:603` and the route-side patch at `routes/messages.py:551-553`). Reconnect snapshot served from the projection. Since nothing has shipped, no data migration — but keep a one-shot import of any existing local ledger or a typed "ledger superseded" notice.
2. **Component catalog single-source** — see W7 (clio-schemas). Backend `_COMPONENT_PROPS`/`_COMPONENT_REQUIRED_PROPS`/`_validate_*_component` become schema-driven; the `create_a2ui_surface` docstring component list is **generated** (kills the `Checkbox`/`CheckBox` class). Until W7 lands, an interim unit test asserts the two dicts share keys and the docstring names match the allowlist.
3. **Validation idiom**: component-shape validation moves to Pydantic/generated models (house idiom, 59 existing models in `gact/types.py`); keep the hand-rolled recursive security sweep (`_validate_value`) — it is fine as-is except:
4. **`clio.diff.v1` path bug**: scope the JSON-Pointer binding rule (`a2ui.py:318`) to actual binding contexts, not every key literally named `path`; test with relative and Windows paths.
5. **`apply_batch` service function** between both entrypoints (`routes/a2ui.py`, `a2ui_tools.py`) and the store — one validate-then-apply implementation.
6. **Version dispatch registry**: negotiate once into `request.state.protocol_version` in the existing middleware (`app.py:1245-1277`), replace the 12 scattered `requests_gact_v3()` forks with a projection registry keyed on version. Single constants: `GACT_V3`, `A2UI_V091` (+ derived wire spelling `"v0.9.1"`) — no bare literals.
7. **`capabilities_to_v3`** (`protocol_v3.py:166-178`): stop hardcoding four flags `True`; carry `x_clio_*` flags through (coordinate the frontend schema change in W11); emit a typed degradation for anything actually dropped.
8. **Persisted-state versioning**: whatever the projection persists reads `protocol_version` on load and branches (copy the era idiom from `tools/mcp_connection_era.py:139-141`); unknown-version records are quarantined with a typed reason, never dropped.

## Phase 3 — gact-tui frontend workstreams

### W8 — Gates + CI truth
- Fix the three red gates: `conversation.tsx` 840→<800 by extracting concerns (not by trimming); restore `a2ui-catalog.tsx`'s registered import in `check_frontend_component_reuse.mjs` registry (or re-add the import); fix the `'3 observations'`→rows assertion in `a2ui-catalog.test.tsx:76`.
- Wire `test:e2e` + `@axe-core/playwright` into `apps.yml`; regenerate Playwright snapshots on Linux CI (drop the win32-pinned PNGs).
- Put `tui/` back under a test target (`go.work` or an explicit make target) — 393 mechanically rewritten files currently build/test nowhere except one Dockerfile.
- Restore `TUI_VERSION`/`TUI_BUILD_REVISION` from `git describe` (Makefile:17-20).
- `apps.yml` release pipeline: **owner decided — restore the full 537-line matrix** (ci job with Playwright + screenshot upload, tauri-debug, native-webview-proof, the 8-variant lite/bundled release matrix with sidecar launcher + sha256 + installer staging, release-web pure-web zip, finalize-notes), adapted to the rebuilt tree layout (`web/`, `packages/core`, `desktop/`) and re-triggering on `clio-desktop-v*` tags. Verify the non-tag jobs run green on the PR; the tag-gated release jobs are validated by workflow-syntax + a dry-run job matrix where feasible (no release is cut in this campaign).
- Add `src/store` and `src/App.tsx`/`main.tsx` to the lint globs (`web/package.json:8`).

### W9 — Delete the 8 client filters; file+fix the server defects they mask
For each: file the server issue with the review evidence, fix server-side in this campaign where the server is ours, then delete the client code and its tests.
1. `lib/connection-health.ts` `materialConnectionDegradations` — render server degradations truthfully (typed chip), delete the filter and the `server→agent` reason rewrite.
2. `conversation-turn-model.ts:26-44` `deduplicateArtifactBlocks` — server defect: duplicate artifact block emission; fix emission, delete dedup.
3. `tool-presentation.ts:171` `isMachineFacingToolTitle` — server/MCP defect: identifier-shaped titles; server supplies display titles (tool-title pipeline exists per #1188/#353), client renders verbatim.
4. `lib/workspace-files.ts` `visibleWorkspaceFiles` — server gains a `hidden`/`internal` flag on file listings; client honors the flag only.
5. `lib/session-child-relations.ts` fabricated messages — server emits real child-relation records; client stops minting `Message` objects.
6. `subagent-card.tsx:113-127` prose scoring — server supplies the child's summary/fact (child answer contract); delete the regex scorer + its self-confirming fixture.
7. `conversation-turn-model.ts:195-210` `'Finalizing the response'` synthesis + `/react step/iu` scrub — **server defect confirmed: server still emits "ReAct step" vocabulary**; fix server summaries (no internal vocabulary on the wire — add to the backend no-settle/vocabulary check), delete the scrub.
8. `conversation-turn-model.ts:174-180` lane lookahead — server labels every text block's `channel` explicitly (backend fix); delete the lookahead.

### W10 — Runtime-auditable override registry (new capability)
- One choke point `reportPresentationOverride({kind, entityId, serverValue, rendered, issue})` in the presentation layer; any surviving heuristic must call it (post-W9 the set should be ~zero; the registry exists so the next one is governed).
- Registry file where every override kind names the server-defect issue it papers over; a check script (same mechanism as `check_frontend_component_reuse.mjs`) asserts registry ↔ call-site parity; **baseline starts at post-W9 count and ratchets to zero**.
- Firings surface in the observability dock (count per session) and structured dev-console logs.

### W11 — Forward-compat + typed degradation
- `.catch()` on all 49 enums in `packages/core/src/v3/schemas.ts` (unknown member → typed `'unknown'` variant rendered as such, never a thrown frame).
- One exported `PROTOCOL_VERSION` (+ `A2UI_VERSION`) from core; both transports and all six literal sites import it. Version-mismatch refusal text names both versions.
- Per-frame `safeParse` in `live-store.applyFrames` — one bad frame costs one frame (typed gap for that entity), never the batch.
- Kill the redundant `envelope.type !== frame.eventName` throw (`reducer.ts:83`) or downgrade to a typed warning.
- Replace the ~56 `as Domain` casts with `satisfies`/inferred types so schema↔domain drift fails to compile.
- Render `surface.error` (decoded, stored, currently never shown); typed unknown-block placeholder case in `MessageBlockView`; Mermaid render failure clears the canvas and shows the reason (`mermaid-preview.tsx:212`, `zoom-pan.tsx:88-90`); wire in the already-written `sanitizeMermaidSvg`; `zoom-pan` image `onerror`; map tile-failure state; app-level error boundary in `main.tsx`.
- `artifact-card.tsx:51-57`: missing `size` → bounded fetch or typed refusal, never an unbounded `GET /bytes`; chart preview cache keyed with a staleness/integrity token once the backend serves one (`preview.data.truncated`/`sampled_rows` rendered from server fields, not re-derived).
- A11y: consume `props.accessibility` in all 15 A2UI renderers; keyboard handlers + toolbar pan for `zoom-pan.tsx`; humanize status strings in `observability-processes.tsx:319` aria-labels; user-facing error codes humanized (`conversation.tsx:290`).
- Em-dash field joins (8 sites, worst `artifact-provenance.tsx:115-117`, `workspace-labels.ts:31-32`) → spans/gap layout per house rule.

### W12 — Render contract restored (owner decision: the NEW grammar is approved)
- The owner has approved the rebuild's transcript grammar as-is — **both Full and Chain (CoT) presentation modes** — as the replacement for CANONICAL-CONVERSATION.md. Codex's job is to **codify it, not retrofit the old grammar**: write the successor acceptance contract documenting the new grammar (turn/iteration structure in both modes, tool cards, reasoning lanes, child/background activity, A2UI surface placement, residual-block fall-through), commit it where the old one lived (or `docs/` successor path), and treat it as owner-locked from that commit forward — structure drift against it is forbidden.
- The old contract's detail rules are NOT reimposed wholesale. Two small polish items carry over unless the approved grammar deliberately chose otherwise (record the choice in the contract either way): the tool-input heading ("Parameters" → "**Arguments**", `tool.tsx:126-128`) and where the tool `thought`/reason renders.
- Fix the stale pointer at `tui/internal/ui/execution_render.go:300` to reference the new contract.

### W13 — Structure + hygiene
- Split `workspace-page.tsx` (795 lines, 36 hooks) **by concern**: `useWorkspaceData` / `useSessionMutations` / `useWorkbenchNavigation` — before the line cap forces a line-split.
- `workbench.tsx`: canvas registry (mirror the A2UI catalog pattern) + `never` exhaustiveness guard, replacing the three ternary ladders.
- `use-session-live-stream.ts`: invalidation off the `for await` path (queue microtask/batch); `event → query-keys` map instead of the hardcoded fan-out; kill the redundant 1.5s `processes` poll (one owner per dataset).
- Typed query-key factory module replacing ~150 inline literals.
- Layer enforcement: `no-restricted-imports` (web may import only `@clio/core/v3`); narrow `packages/core` exports map to `./v3`; delete `packages/core/src/{client,store,wire}` (6.5k lines) + their 20 tests.
- Remove ~8 dead npm deps (`@rive-app/react-webgl2`, `media-chrome`, `embla-carousel-react`, `react-jsx-parser`, `ansi-to-react`, `@streamdown/code`, orphaned `@dnd-kit/*` + `kibo-ui/code-block` subtree with `@shikijs/transformers`/`react-icons`); four lazy-load wins (resource-viewers, xyflow wrapper, time-series-plot, break `a2ui-catalog`'s static code-block import).
- `data-grid-table.tsx`: fork explicitly into CLIO ownership (it carries load-bearing TanStack v9 fixes) — enters lint+size+test scope — or upstream the fix.
- Fix `context-canvas-panel.tsx:294` invalid `hsl(oklch(...))`.

### W15 — Port the UI-level flowcept work (feat/flowcept-observability)
- gact-tui branch `feat/flowcept-observability` (1 commit e4e4b877, "provenance provider views") targets ONLY deleted old-tree paths (`apps/core/src/client/execution_provenance.ts`, `apps/web/src/observability/{ExecutionGraph,Observability,executionProvenance}.*`, `apps/web/src/session/SessionView.tsx`). **Do not merge the branch** — a merge resurrects orphans in dead directories. Port it, treating the branch as the data/action inventory (the same method used for the rest of the rebuild):
  1. Data layer: a v3 repository + zod schemas in `packages/core/src/v3` for the execution-provenance endpoints the backend now serves on develop (flowcept/CMF work, #1247).
  2. Views: re-express the provenance provider views in the new observability stack (`observability-evidence.tsx` / `workflow-graph.tsx` are the natural homes); follow W11 typed-degradation rules for missing/partial provenance.
  3. Tests ported from `execution_provenance.test.ts` / `execution-provenance.test.tsx` semantics onto the new seams.
  4. Acceptance: provenance views render real Flowcept/CMF data in the live qualification run (Verification #4/#5); then close `feat/flowcept-observability` as superseded (delete the branch, note it in this doc).

### W14 — Test integrity
- Port the provenance machinery to v3: `spec_vocabulary.test.ts` against the v3 event set; document protocol v3 + A2UI events in `contract/SPEC.md`; un-exclude `live-clio.test.ts` (service container in CI) or generate frontend fixtures from a backend golden-frame capture.
- Dedicated tests for `tool-presentation.ts` and `child-agent-presentation.ts` (the surviving, registry-governed parts); delete self-confirming fixtures with the heuristics they validated (W9.6).

## Phase 4 — W7: the cross-repo contract (clio-schemas)

1. Add the **two closed vocabularies** to `clio-schemas` as JSON Schema: (a) the v3 message-block union (13 types), (b) the A2UI 0.9.1 component catalog — component names, props, required sets, per-component constraints (node/edge maxima, series shapes), and the client-action envelope. Target a patch-level bump (0.2.x, additive); **owner has pre-approved a minor bump (0.3) if structurally unavoidable — no mid-campaign stop**.
2. Backend consumes it: schema-driven validation replaces the parallel dicts (W4.2/W4.3); tool docstring generated.
3. Frontend consumes it: fix `.github/workflows/schema-ts-gen.yml` (trigger on `packages/core/**` + the schema pin, output committed, not `/tmp`); derive `messageBlockSchema` + catalog zod from generated types.
4. **Retarget the two conformance gates at v3**: `contract/conformance/client.go:94` sends `X-GACT-Version: 0.3`; `spec_vocabulary.test.ts` imports the v3 vocabulary. (This is the ~1-day change that would have caught every seam divergence — do it FIRST within this phase, before the generation work.)
5. Client-action validation: known-keys tolerance with typed rejection instead of exact-set equality (`a2ui.py:515-517`); `X-A2UI-Version` required on `/a2ui/actions` like `/a2ui/messages`.

## Phase 5 — Marketplace + clio-kit

**Marketplace** (PR → main):
- Fix `earthscope-flat/AGENT.md:6` (still advertises the removed sonnet pin).
- Extend session-model inheritance to the three remaining forced packs (`cluster-operator/experts/operator.md:8`, `phenotype/experts/main.md:8`, `spotter-ai/experts/spotter_watcher.md:9`) — **owner decided: all packs inherit; no pin survives** (cost posture is safe: the session-level claude_code default is already sonnet).
- Add a repo lint script: no `default_model:` in pack frontmatter without an adjacent justification comment; run it in marketplace CI.
- Fix `scripts/live_gate_observe_1000.py`: default `--blueprint-path` points at a path that doesn't exist; remove the session-level `--model sonnet` pin so the gate can actually detect a re-pinned blueprint.
- After merge: clio-agent re-pins `external/clio-agent-marketplace` to the **main** merge commit (the current pin is a feature-branch-only commit that dangles when the branch is deleted) and registers the submodule (`git submodule status` currently shows `-`).

**clio-kit** (PR → main, matching its release-line base; forward-merge main→develop after):
- Add a `maintain_after_build` test with `max_cache_bytes` configured (the `else:` branch has zero coverage; the commit repurposed the one test covering it).
- `BudgetReport.total_bytes: int | None` (no fabricated zero); test for invalid-config → skip with both typed events.
- PR description states plainly: conditional skip, not non-blocking (the branch name overstates).

## Phase 6 — Merge choreography (strict order)

1. **flowcept → clio-agent develop** — ✅ DONE (already landed via PR #1248 + edge-close train; branch deleted).
2. **marketplace → main** (green PR). clio-agent re-pins to the main commit.
3. **clio-kit → main** (green PR); forward-merge main→develop.
4. **clio-agent `codex/gact-a2ui-v091-producer` → develop**: after Phases 1+2+4 complete on the branch. PR CI must be fully green: pytest (`-m "not integration"`, cov ≥78), ruff, mypy, `check_file_size`, `check_silent_fallbacks`, `check_no_class_in_function`, route-count guardrail, env-reference check. The gact-tui submodule pin stays at v0.9.9 (release pins move only at release time; we stop at develop).
5. **gact-tui `codex/gact-tui-node-revamp` → develop**: after Phases 3+4 complete — **including W15** (the ported flowcept observability views; the develop merge supersedes `feat/flowcept-observability`, which is then deleted). PR CI fully green: oxlint (`--deny-warnings`), file-size + reuse checks, vitest, Playwright e2e + axe (Linux snapshots), Go build/vet/test **including `tui/`**, conformance gates (now v3-targeted).
6. Post-merge: delete the four feature branches; prune stale locals (`fix/172-survivors` in clio-relay is unrelated — leave it); clean any worktrees/caches this campaign created; update `docs/design/roadmap.md` and the memory/plan docs to reflect the landed state.

## Verification (the definition of done)

1. **CI green on every PR** — the merge requirement Alice set. No skipped tests that should run; no baseline raises.
2. **The proven bugs re-probed**: the three empirically-demonstrated failures (A2UI ledger corruption wipe, session-defaults 501, non-atomic batch / resurrectable delete) re-run against the fixed branch — probe scripts from the review live in this session's scratchpad; port them into the test suite where not already covered.
3. **Cross-repo drift gates prove themselves**: temporarily reintroduce the `Checkbox` typo locally → the generated-schema path or interim sync test must fail. The v3 conformance gate run against a live backend stream passes.
4. **One live end-to-end qualification** (validation, not discovery — batch fixes first, one live run at the end): a real EarthScope/NDP session and a SPOTTER session against the merged stack — A2UI surfaces (map, table, Mermaid revision including a failing revision → visible typed error, artifact-backed time series with truncation notice), a >12k-char failing tool call rendering as **failed**, a spawned child + skill-driven child visible in the transcript, provider identity shown correctly on a codex-provider session (doctor READY, no `****` joins in reasoning).
5. **Flowcept/CMF re-qualification** on the merged base (Phase 1.4 rerun post-merge on develop).
6. **Frontend visual acceptance**: run the real app against the merged backend; screenshot checkpoints of the transcript grammar per the owner-signed W12 contract.

## Owner decisions (all resolved — recorded here so Codex never has to ask)

1. **Everything pre-merge**: the full fix list, including the quarter-tier architecture items, lands before the develop merges.
2. **Flowcept**: the other Claude session lands `feat/flowcept-provenance` → develop first; Codex absorbs (Phase 1).
3. **No release**: the campaign stops at develop.
4. **W12**: the rebuild's new transcript grammar (Full + Chain/CoT modes) is approved as the render contract; Codex codifies it as the owner-locked successor to CANONICAL-CONVERSATION.md.
5. **W8**: restore the full apps.yml release matrix, adapted to the new tree.
6. **W7**: clio-schemas targets a patch bump; a minor is pre-approved if unavoidable.
7. **Marketplace**: all packs inherit the session model; no pins survive.
