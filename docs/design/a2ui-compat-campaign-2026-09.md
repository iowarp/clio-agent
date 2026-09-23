# A2UI 0.9.1 compatibility campaign (2026-09)

**Status:** ACTIVE — approved 2026-09-16. Umbrella: [#1363](https://github.com/iowarp/clio-agent/issues/1363).
**Base:** develop @ `1e5d0341` (v0.9.2 + release fixups). Branch `feat/a2ui-compat` in
clio-agent, clio-schemas, gact-tui, clio-agent-marketplace.
**Supersedes:** the Codex proposal `external/gact-tui/docs/review/a2ui-v091-compatibility-campaign-2026-08-30.md`
(never executed; validated claim-by-claim below).
**Authority:** the A2UI protocol — google/A2UI `specification/v0_9_1` (current) and
`specification/v1_0` (release candidate), pinned at commit `0086493c40b119a4143fe15197006678467cad60`
and vendored into clio-schemas. Not this document, not the Codex document.

## Goal

CLIO becomes a truthful A2UI 0.9.1 agent and renderer, designed so 1.0 lands as an added
version, and CLIO's existing A2UI moves into shareable, protocol-compliant catalogs that
blueprints declare and packs ship, exactly the way MCP servers are declared and shipped.

## Red baseline (audit 2026-09-16, at v0.9.2)

| Codex-doc gap | Status at v0.9.2 | Evidence |
|---|---|---|
| Single hard-coded catalog | open | `gact/protocol/constants.py:9`; `gact/a2ui.py:369,484`; `a2ui-surface.tsx:178` |
| No catalog negotiation in capabilities | open | `gact/protocol/v3/capabilities.py:27,90`; transports send only `X-A2UI-Version` |
| Closed global component/action union | open | clio-schemas `a2ui_v091.py:491-523,535-541` |
| `functionCall` rejected | open | `gact/a2ui.py:285` |
| Global `LOCAL_ACTIONS` switch | open | `a2ui-surface.tsx:66`, `use-a2ui-local-actions.ts` |
| `form.submit` acknowledges only | open | `routes/a2ui.py:249` |
| `agent.submit` text-only / 409 busy | **closed (mostly)** | busy → `enqueue_user_steer` (3406ca26); structured context as `a2ui_action_context` (94a2c71a); text still required |
| No durable correlated action lifecycle | open | `routes/a2ui.py:149` correlation unread; no idempotency |
| `sendDataModel` not carried | open | accepted key only, `gact/a2ui.py:355` |
| `VALIDATION_FAILED` not returned | open | zero occurrences in any repo |
| Local form state across live updates | open, actively broken | `a2ui-surface.tsx:176-191,285` |
| Waiting-user resume | open | no `question_id` path in `routes/a2ui.py` |

Solid and kept: transcript-owned surface persistence/replay/tombstones
(`gact/a2ui_store.py`, `routes/session_a2ui_preservation.py`), the security safety walk
(`gact/a2ui.py:221-299`), version negotiation on both action doors, the untouched official
action envelope, the EarthScope skills' agent-facing guidance.

### Closure (S9, 2026-09-17)

Every red-baseline row above is closed on develop (`d433b41f` clio-agent /
`3619a715` gact-tui). Mapping to the slice that closed it — see the Slice ledger
above for each slice's own issue/PR reference:

| Red baseline gap | Closed by |
|---|---|
| Single hard-coded catalog | S2 (`gact/a2ui_catalogs/` registry; deletes the `CLIO_A2UI_CATALOG_ID` equality) |
| No catalog negotiation in capabilities | S3 (`gact/a2ui_capabilities.py`, per-session client memory, catalog selection) |
| Closed global component/action union | S1 (clio-schemas 0.3.0 official shapes; deletes the 30-model union and the closed action Literal) |
| `functionCall` rejected | S2 (server stops rejecting it) and S6 (gact-tui client runtime executes catalog functions, `kernel-catalog-functions.ts`) |
| Global `LOCAL_ACTIONS` switch | S6 (gact-tui client runtime rewrite; deletes `use-a2ui-local-actions.ts` and the local branch in `a2ui-surface.tsx`) |
| `form.submit` acknowledges only | S5 (`gact/a2ui_actions/` dispatcher + durable record; deletes the `form.submit` echo) |
| `agent.submit` text-only / 409 busy | closed mostly pre-campaign (busy -> `enqueue_user_steer`, structured `a2ui_action_context`); S5 removes the remaining `context.text` requirement |
| No durable correlated action lifecycle | S5 (`gact/a2ui_actions/record.py`, idempotent record + fold + lifecycle event payload) |
| `sendDataModel` not carried | S3 (`a2ui_capabilities.py`'s `A2UIClientDataModel`), refined by S5 (`client_state.py` per-surface data-model filtering) |
| `VALIDATION_FAILED` not returned | S2 (server-side `jsonschema` validation) and S6 (gact-tui client processor posts `VALIDATION_FAILED` back on error, `processor-store.ts`) |
| Local form state across live updates | S6 (gact-tui client runtime rewrite: local-action/pre-validation/revision-remount branches deleted from `a2ui-surface.tsx`; state ownership moves to `processor-store.ts`, covered by `processor-store-reconcile.test.tsx`) |
| Waiting-user resume | S5 (`ask_user` gains `surface_id`; `user_question_resume.py` forwards `question.answer_metadata` onto the resumed turn) |

## Codex-doc validation ledger

OK = matches the protocol; EXT = CLIO extension the protocol is silent on; WRONG = contradicts
the protocol or invents a shape; MISSED = protocol fact the doc did not anticipate.

| # | Doc claim | Verdict | Protocol truth |
|---|---|---|---|
| 1 | `form.submit`/`agent.submit` are CLIO conventions | OK | No standardized submit name; agents define `event.name` |
| 2 | Functions are local renderer ops, never notify the agent, no code from the wire | OK | `functionCall` executes locally, never emitted; 1.0 adds catalog-declared RPC |
| 3 | catalogId is a negotiated identifier; URI ≠ fetch; known at deploy time | OK | Verbatim in the catalogs concept page |
| 4 | `sendDataModel` → full model as transport metadata | OK | Metadata key `a2uiClientDataModel {version, surfaces}`; sent only to the creating server; orchestrators strip it before sub-agents |
| 5 | Protocol defines event dispatch, not LLM-turn orchestration | OK | Transport-agnostic; correction loop is application-level |
| 6 | 1.0 RPC out of scope | OK | 1.0 is an RC; 0.9.1 is "current production" |
| 7 | `A2UICapabilities {protocol_versions, producible_catalog_ids, inline_catalogs enum, client_functions, send_data_model, validation_feedback, replay, degradations}` | WRONG | Client: `a2uiClientCapabilities {"v0.9": {supportedCatalogIds[], inlineCatalogs?[]}}`; agent: `{"v0.9": {supportedCatalogIds[], acceptsInlineCatalogs}}`. Functions, data model, validation feedback are mandatory renderer behaviours, not flags |
| 8 | Manifest with `x_clio_*` fields inside the catalog | WRONG | The 1.0 catalog file is `additionalProperties:false`; CLIO packaging is a sidecar |
| 9 | `producer_guide_ref` | MISSED | 1.0 embeds `instructions` (markdown) in the catalog |
| 10 | Inline catalogs disabled in production | OK as policy | "supported but not recommended in production"; `acceptsInlineCatalogs` defaults false |
| 11 | Renderer checklist | OK | Progressive render on a valid `root`, adjacency list, templates, two-way binding, functions/checks, ValidationFailed, delete cleanup |
| 12 | `VALIDATION_FAILED {code, surfaceId, path, message}` | OK | `client_to_server.json`: validation error (strict) or generic error |
| 13 | Durable action record with lifecycle/idempotency/correlation | EXT | Kept as a transcript part in the existing store, projected by the unified interactions substrate |
| 14 | `LOCAL_ACTIONS` events filtered client-side | WRONG | Local behaviour is a catalog `functionCall`; an `event` always goes to the agent. These become catalog functions |
| 15 | Checks disable submission; unknown function fails closed | OK | Library `CheckableSchema` → `isValid`; unknown function → `A2uiExpressionError` |
| 16 | `x-a2ui-version` header + 406 | EXT | Envelope `version` is normative; the header stays as GACT pre-flight |
| 17 | Invented "compatibility-proof catalog" | WRONG-ish | The official Basic catalog, its 43 examples and the ajv corpus are the conformance proof; the pack-borne catalog is the composability proof |
| 18 | Local state survives unrelated server updates | EXT | Product rule; today broken |
| 19 | 1.0 seam = "versioned boundaries" | MISSED specifics | 1.0: per-component `catalogId` mixing; `createSurface.catalogId` optional, `theme` removed, inline components/dataModel; `updateDataModel.value` required (null deletes); catalog `protocolVersion`, `instructions`, functions map + `allowedCallers`; `ValidationResult` checks; UAX#31 names (`clio.map.v1` is illegal); action `userMessage` + `metadata`; RPC; renderer/agent terms; MIME `application/a2ui+json`. 0.9 catalogs are unusable on a 1.0 runtime |
| 20 | A2A metadata is the binding | MISSED | CLIO's transport is GACT; the GACT binding is written in S3 |
| 21 | Processor built with one catalog | OK (gap) | The library selects by `catalogId` over `catalogs[]`, exposes `getClientCapabilities()` / `getClientDataModel()`, runs functions and checks |
| 22 | Five special event names with server branches | WRONG | One `event` shape, agent-chosen names, no "larger actions". The names go; destinations are declared data |

## Owner decisions

1. **Catalogs are defined in blueprint semantics and downloaded into CLIO like MCP servers.**
   `AGENT.md a2ui_catalogs:` declares them; the pack ships `catalogs/<name>/{catalog.json,
   catalog.clio.json, instructions.md}`; install copies them; the server registers them keyed
   `(catalogId, protocolVersion)`. This is the protocol's own model: implementations at deploy
   time, vocabulary at runtime. A pack catalog names components the renderer already
   implements (official Basic + CLIO scientific set), optionally under its own names with prop
   presets, plus its own events and instructions. No sandbox. Pack-shipped renderer *code* is
   out of scope (the protocol "avoids sending executable code").
2. **Official shapes verbatim.** `a2uiClientCapabilities`, `a2uiClientDataModel`,
   `supportedCatalogIds`, `acceptsInlineCatalogs`, `VALIDATION_FAILED`. CLIO metadata lives in
   the sidecar `catalog.clio.json`: `{catalogId, protocolVersion, trust:{source}, implements:
   {<Name>: {kernel, presets?}}, events: {<name>: {destination: agent|permission|run,
   context_schema?}}, instructions}`.
3. **Validation = official JSON Schema at runtime** (`jsonschema` Draft 2020-12 with a
   preloaded `referencing.Registry`; no network). The CLIO safety walk stays as trust policy.
   The client's reference processor already validates per catalog.
4. **Durable action record = an `a2ui_action` transcript part** beside the surface parts. No
   new store. The `/lastAction` data-model ack is deleted.
5. **Disclosure = catalogs as skill directories.** `SKILL.md` (instructions + index) rides the
   existing skills block; `load_skill(skill_id, file="catalog.json#/components/Button")` loads
   one component schema. No new tool. Validation errors name component + JSON pointer.
6. **All events go to the agent** unless the catalog sidecar declares `permission` or `run`.
   The five legacy names, `SERVER_ACTIONS`/`CLIENT_ACTIONS`, the `context.text` requirement and
   the `form.submit` echo are deleted. Old surfaces replay unchanged.
7. **Client capability advertisement lives once** in `packages/core`; browser and Tauri are
   identical by construction. The client advertises every installed catalog; the active
   blueprint gates what a session may produce.
8. **Library pin stays 0.10.6 for the claim**; the 0.11.x upgrade is its own slice.
9. **clio-schemas → 0.3.0** (public exports removed).
10. **New catalogs use UAX#31 identifier names**; `clio-workspace/v1` keeps its dotted names
    and is 0.9.1-only.
11. **URL scheme enforcement: server on literals; the renderer's kernel media/artifact
    components enforce the same allowlist on bound values at render time and report
    VALIDATION_FAILED (S6 deliverable).** A property like `Image.url` is a `DynamicString`, so
    a data binding (`{"path": "/productImage"}`) or a declared `functionCall` resolves
    client-side; the server boundary cannot see the resolved value and no longer requires the
    literal (adversarial S2 review). Only a literal string URL is scheme-checked server-side.

## Architecture

```
blueprint AGENT.md a2ui_catalogs: {name: catalogs/<name>}         (declare, like mcp_servers)
   └─ pack/catalogs/<name>/{catalog.json, catalog.clio.json, instructions.md}   (ship)
install_agent_blueprint copies the pack (+checksum)                             (install)
server CatalogRegistry (catalogId, protocolVersion): builtin{basic, clio-workspace} ∪ packs
   ├─ validate_server_message → jsonschema per catalog + safety walk
   ├─ session producible set = builtin ∪ active blueprint's
   ├─ /v1/capabilities a2ui {"v0.9": {supportedCatalogIds, acceptsInlineCatalogs: false}}
   ├─ GET /v1/sessions/{sid}/a2ui/catalogs → files + sidecars (client registry source)
   └─ catalogs exposed as skills → skills block + load_skill
client CatalogRegistry: kernel impls (basicCatalog from @a2ui/react + clio components)
   ├─ per catalog: new Catalog(id, impls via sidecar.implements, functions)
   ├─ one MessageProcessor per surface, incremental; onError → VALIDATION_FAILED
   └─ metadata.a2uiClientCapabilities / a2uiClientDataModel on messages and actions
dispatcher: event → a2ui_action part; idle → turn | running → steer | waiting_user → answer
   permission/run destinations → existing owners; idempotent by key; correlation kept
```

## Slice ledger

Each slice merges to develop on CI green before the next starts. Fill in as work lands.

| Slice | Scope | Issue | PR | Head SHA | Evidence | Status |
|---|---|---|---|---|---|---|
| S0 | Campaign doc, red baseline, vendored corpus, `jsonschema` dep | | | | | in progress |
| S1 | clio-schemas 0.3.0: official shapes, catalog files, sidecar; delete union + Literal | | | | | |
| S2 | Server registry, per-catalog validation, blueprint declaration; delete constant, action sets, `required_context`, functionCall rejection. Adversarially reviewed and fixed: registry/discovery caching, the 126-message official corpus, four safety-walk protocol-conformance rulings, typed reasons for every swallowed exception. Accepted deviation: `routes/interactions.py`'s `_surface_actions` reports every declared event name found in a surface's messages rather than filtering to catalog-sidecar-declared destinations only — the catalog file is the allowlist for VALIDITY (S2), not for this UI-hint projection's vocabulary. | [#1365](https://github.com/iowarp/clio-agent/issues/1365) | (not pushed) | (local commits, see `git log`) | `gact/a2ui_catalogs/` (registry/builtin/blueprint/activation/validation/reasons/routes), `tests/test_gact/test_a2ui_catalog_registry.py`, `tests/test_gact/test_a2ui_corpus_conformance.py`, `tests/fixtures/a2ui_packs/minimal/`, `tests/fixtures/a2ui_corpus/v0_9_1/` (126/126 official messages validate); clio-schemas pinned to 0.3.0 @ `dd34c2b28b5f45c8c387979658eef7ebd5ed5fba` | implemented locally, PR pending |
| S3 | Official capabilities (`gact/a2ui_capabilities.py`), per-session client memory (`Session.metadata`, no fifth store), catalog selection, sub-agent stripping at every parent->child metadata site, GACT binding doc. Deviation: the corpus-conformance fixtures (`tests/fixtures/a2ui_corpus/`, `tests/fixtures/a2ui_packs/`) were vendored without a `.gitattributes` LF pin, so a Windows checkout with `core.autocrlf=true` silently corrupted their bytes and broke all 43 hash-manifest tests (pre-existing since S0, not introduced here) — fixed in-slice per "failing tests are in scope": `.gitattributes` entries + working-tree renormalization; no code/behavior change. | [#1369](https://github.com/iowarp/clio-agent/issues/1369) | (not pushed) | (local commits, see `git log`) | `gact/a2ui_capabilities.py`, `gact/a2ui_catalogs/routes/a2ui_capabilities.py`, `docs/gact/a2ui-binding.md`, `tests/test_gact/test_a2ui_capabilities.py` (36/36); full exit-gate suite (`test_a2ui*.py test_gact_v3.py test_sessions_api.py test_messages_sessions_query_params.py`) 282/282 green | implemented locally, PR pending |
| S4 | Producer tools (`gact/a2ui_producer/`: create/update-components/update-data-model/delete, typed refusal shape, never exceptions), catalogs disclosed as generated skills (`gact/a2ui_catalogs/skills.py`, `gact/skills.py`'s new `catalog` scope + `SkillRef.body_provider`/`extra_dirs`), `load_skill` JSON-pointer fragment reads (`agents/skill_runtime.py`); delete `a2ui_tools.py` whole. `CatalogEntry` gained `name` (short slug) and `catalog_file_path` (distinct from `root_path` — the vendored Basic catalog's asymmetric layout, `builtin.py`'s own docstring — needed a real path to `catalog.json`, and `SkillRef.extra_dirs` so its skill exposes BOTH its sidecar directory and that path). Catalog-skill auto-declaration and discovery both resolve through the session's own `session_catalog_resolver`/`session_producible_catalog_ids` (not a bare `registry.installed()` walk), so a session's PATH-activated pack catalog is disclosed as a skill exactly when it is actually producible. Surface-kind presentation labels (Input/clio.*/Text) are derived from the resolved catalog (Checkable composition + sidecar kernel), not a hardcoded name table. An explicit `create_a2ui_surface` `catalog_id` for a NEW surface always crosses `select_catalog`'s client-preference gate (a preference, never a bypass). Trimmed `builtin_skills/present-interactive-analysis/SKILL.md` to when/why guidance, prop lore removed. Adversarially reviewed and fixed: items 1 (BLOCKING, the `catalog_id` bypass), 2 (catalog-derived kind labels), 3 (Basic catalog's second bundled root), 4 (typed `scan_errors` for the session-scoped empty-catalog-scope case), 6 (builtin-tools allowlist coverage test), 7 (the `test_spawn_child_turn_never_forwards_renderer_metadata` timing flake — `LITELLM_LOCAL_MODEL_COST_MAP` pinned in `tests/conftest.py` + the test waits on `AgentTaskRegistry.event(task_id)` instead of a bare sleep-poll; verified 10/10 with `pytest-repeat`). | [#1370](https://github.com/iowarp/clio-agent/issues/1370) | (not pushed) | (local commits, see `git log`) | `gact/a2ui_producer/` (`_common.py`, `_emit.py`, `_presentation.py`, `_refusal.py`, `create.py`, `update_components.py`, `update_data_model.py`, `delete.py`), `gact/a2ui_catalogs/skills.py`, `tests/test_gact/test_a2ui_producer.py` (12/12), `tests/test_gact/test_skill_runtime.py` (36/36), `tests/test_gact/test_agent_blueprints.py` producer-tool-allowlist test; full exit-gate suite (`test_a2ui*.py test_skill_runtime.py test_interactions.py`) 279 passed, reproduced twice, deterministic | implemented locally, PR pending |
| S5 | Dispatcher on the interactions substrate; delete `/lastAction` ack, `context.text`, `form.submit` echo. Owner package `gact/a2ui_actions/` (`record.py` durable idempotent record + fold + lifecycle event payload, `dispatcher.py` moved out of `routes/a2ui.py`, `delivery.py` the shared idle/running/waiting_user agent lane — also used by a VALIDATION_FAILED repair delivery, `narration.py`, `client_state.py` data-model per-surface filter + error-envelope ingestion routed BEFORE the surface lookup). clio-schemas bumped to 0.3.1 (`CatalogSidecar._EventRoute.operation`, required for `destination: "run"`). `A2UISurfaceRecord.actions[]` (S5's own field) folds `a2ui_action` transcript parts, same ledger as `a2ui` surface parts, preserved by `session_a2ui_preservation.py` identically. `ask_user` gained `surface_id` for waiting_user correlation; `user_question_resume.py` forwards `question.answer_metadata` onto a resumed turn so S5 context survives the resume. `consumed` hooked at `turn.py::_run_turn_in_background` (fresh/idle-redriven turns) and `steer_delivery.py::compose_steer_block` (mid-turn drain). `routes/interactions.py`'s `_a2ui_interactions` now derives pending/answered from the latest action record, not `/lastAction`. **Adversarial review (two rounds) fixed in-slice**: the idempotency check-and-persist is now atomic inside `A2UIStore.persist_action_part`, under the SAME per-session lock `apply_batch_outcome` uses — a concurrent double-submission proven closed under a `threading.Barrier`-synchronized reproducer, exactly one record and one turn; every delivery owner call (`answer_user_question`/`_start_background_user_turn`/`enqueue_user_steer`/`resolve_permission`/`cancel_session_state`/`retry_turn_action`) is wrapped so an unexpected raise durably fails the record before the same exception propagates; a `VALIDATION_FAILED` report on a surface the session does not own is a persisted dead end (`a2ui_error_surface_unknown`), never a re-drive target; the repair budget only counts attempts that actually reached `delivered`/`consumed`; a duplicate of a `failed` record re-raises the original typed refusal; the sidecar's `events[<name>].context_schema` is now enforced server-side (`jsonschema` Draft 2020-12, typed `a2ui_event_context_invalid`); `_CancelDepsShim` is deleted in favor of threading the real `deps`. | [#1372](https://github.com/iowarp/clio-agent/issues/1372) | (not pushed) | (local commits, see `git log`) | `gact/a2ui_actions/` (`record.py`, `dispatcher.py`, `delivery.py`, `narration.py`, `client_state.py`), `gact/a2ui_catalogs/validation.py` (`validate_event_context`), `tests/test_gact/test_a2ui_actions.py`, `tests/test_gact/test_a2ui_actions_review.py`, rewritten cases in `tests/test_gact/test_a2ui_v3.py` and `tests/test_gact/test_interactions.py`; full exit-gate suite (`test_a2ui*.py test_interactions.py test_loop_thread_writes.py test_session_rollback.py test_compaction.py`) 313 passed | implemented locally, PR pending |
| S5b | Live-gate finding (#1363, "Live-gate findings" comment): on Codex, a resumed turn read S5's legacy `A2UI event: <name>` + JSON narration as a REPORT, not a REQUEST, and never staged the selected stations — the event's MEANING is the pack author's to declare, not clio's to infer from prose. clio-schemas bumped to 0.3.2 (`CatalogSidecar.events[<name>].narration`, a template over the resolved context whose placeholders are validated against `context_schema.properties` at sidecar-construction time; `clio_schemas.a2ui.sidecar.render_narration(route, context) -> str \| None`, unknown placeholders left literal, lists/dicts rendered as compact JSON). `gact/a2ui.py::validate_client_action` now carries the resolved sidecar `route` itself on the parsed action (alongside `destination`/`declared`/`operation`) so `narration_for` never re-resolves the catalog. `a2ui_actions/narration.py::narration_for` renders the declared template (bounded to `MAX_NARRATION_BYTES` with the existing truncation marker) followed by the canonical context JSON on an UNBOUNDED second paragraph — the structured object always travels in text, even for a text-only provider — falling back to the unchanged S5 name+context form when no route declares a narration. Every delivery lane (start/steer/resolve_question) already shared ONE `narration_for` call site in `dispatcher.py`, so no lane-specific wiring was needed; `VALIDATION_FAILED` repair delivery (`repair_narration`) is untouched. The undeclared fallback records a new typed reason, `a2ui_event_narration_undeclared`, ONCE per (session, event name) via `CatalogRegistry.record_narration_undeclared_once` (a small per-session `set`, bounded at the same ring size as the existing per-session reason ledger) — distinct from `a2ui_event_destination_undeclared`, which still records per action. | [#1363](https://github.com/iowarp/clio-agent/issues/1363) | (not pushed) | (local commits, see `git log`) | `gact/a2ui.py`, `gact/a2ui_actions/narration.py`, `gact/a2ui_actions/dispatcher.py`, `gact/a2ui_catalogs/registry.py`, `gact/a2ui_catalogs/reasons.py`, `tests/test_gact/test_a2ui_actions_narration.py` (4/4, failing-first against the pre-S5b tree); full gate suite (`test_a2ui*.py test_interactions.py`) 277 passed; `ruff check`/`ruff format --check`, `mypy src/` (633 files), both file-size/class-in-function ratchets clean | implemented locally, PR pending |
| S6 | Client runtime: registry, incremental processor, functions, capabilities, errors, lifecycle. Owes: kernel media/artifact components enforce the URL scheme allowlist on bound/resolved values at render time and report VALIDATION_FAILED (owner decision 11 — the server only checks literals as of S2). | | | | | |
| S7 | EarthScope pack catalog: composability proof + live gate. **clio-agent half only** (deliverables 4/5/6's clio-agent-side pieces; deliverables 1-3 — the catalog, blueprint declaration, and skill rewrite — are marketplace#69's own scope, landed there and consumed here via `external/clio-agent-marketplace` pinned to `ed3725a`, the S7 pack commit; deliverable 6's marketplace-README half is marketplace#69's own scope too). No `src/` change in this slice (tests + docs only, per the composability claim itself). `tests/test_gact/test_a2ui_pack_composability.py` (10 tests) installs the REAL pack (not a fixture) into an isolated user dir, activates it, and proves — against an unmodified server — catalog discovery (`GET /v1/sessions/{sid}/a2ui/catalogs` producible with file+sidecar+instructions), server-wide (`GET /v1/capabilities`) and client-preference (`select_catalog`, pack listed first) negotiation, `create_a2ui_surface` rendering the pack's own worked example (regex-parsed out of `instructions.md`, never hand-copied, so the test cannot drift from the docs) on an empty `catalog_id`, the `earthscope.stations.selected` dispatch matrix (idle delivery with `a2ui_action_context` verbatim on the started turn's user message, duplicate submission collapsing to one record, `stationIds: []` refused `422 a2ui_event_context_invalid`), the generated skill `a2ui-catalog-earthscope-stations` in the session's skills block with `load_skill(..., file="catalog.json#/components/StationPicker")` returning the `variant: multipleSelection` definition, and a `waiting_user` resume correlated by `surface_id`. `tests/test_real_cases/test_earthscope_interactive.py` gained three live scenes (`test_earthscope_a2ui_{idle,queued,waiting_user}_selection`, written under the file's existing `real_case`/`CLIO_RUN_LIVE` gating and pre-allow-policy `gact_server` fixture; not run in this slice) driving the SAME pack on "Show me the GNSS stations around Los Angeles," asserting on the live surface's own data model and `a2ui_action` records / tool-call parts, never prose; the module docstring documents the exact `claude_code`/`codex` invocation per the live-test rule. `docs/gact/a2ui-binding.md` gained a "Pack-borne catalogs" worked-example section pointing at this pack. | [marketplace#69](https://github.com/iowarp/clio-agent-marketplace/issues/69) | (not pushed) | (local commits, see `git log`) | `tests/test_gact/test_a2ui_pack_composability.py` (10/10 passed, plus the full `test_a2ui_catalog_registry.py` co-run 38/38), `tests/test_real_cases/test_earthscope_interactive.py` (extended module docstring + 3 new scenes; `--collect-only` gate 13/13 collected, live run pending a grind session), `docs/gact/a2ui-binding.md` | implemented locally (clio-agent half), PR pending; live scenes unrun |
| S8 | Hardening (clio-agent half, issue #1374 deliverables 1-6): pre-campaign (v0.9.2) transcript replay + legacy `agent.submit`/`form.submit`/`approval.respond`/`run.*` dispatch, uninstalled-catalog replay reinstalled-without-restart, two-session/two-blueprint isolation in one workspace, compaction/undo/delete/full-restart durability of `a2ui`+`a2ui_action` parts incl. no-duplicate-delivery after restart, reason-ring (256) + surface-message-retention bounds under sustained load, desktop parity (server side, `docs/gact/a2ui-binding.md`). Ledger honesty (adversarial-review items 4/8/10): the v0.9.2 transcript fixture (`tests/fixtures/a2ui_transcripts/v0_9_2_workspace_surface.json`) is CONSTRUCTED from the pre-campaign wire shapes recorded in git history, not a vendored capture of a real owner session — no such capture exists to vendor. The load bounds test actually runs 60 surface updates and 260 actions (not a larger claimed number); it also has a documented, unfixed SEPARATE cost (MessageStore disk flush is O(n) per write; `sessions_path=None` does not mean in-memory, see that test module's docstring). The S7 EarthScope pack declared `requires: {clio_agent: ">=0.9.5"}` at the time; this worktree ran a 0.9.4 source checkout, so the pack was correctly REFUSED here — the floor working as designed. (Owner ruling 2026-09-23: the campaign ships as the 0.9.4.15 patch release, so the pack floor is now `>=0.9.4.15`, marketplace v0.6.4.) Plus the two S7-review additions: `requires: {clio_agent: ">=X"}` PEP 440 floor enforcement at `validate_agent_blueprint_path` + session activation (typed `blueprint_requires_newer_clio_agent`, recorded like `blueprint.resolution.degraded`) in the new owner module `gact/agent_blueprint_requires.py`; and actionable, "what is true + what to do instead" refusal wording for every producer-tool `ok:false` reason (`gact/a2ui_producer/_refusal.py`) plus the typed `a2ui_producer_refusal_repeated` observability reason for a refusal recurring within one turn. Also from the #1363 umbrella live-gate comment item 3: `agent_blueprint_refresh.sync_local_registry_packs` now checksum-compares an already-installed pack against its source and reinstalls on a genuine change (never a silent stale copy), while still refusing to clobber local edits. gact-tui companion (library 0.11.x, client replay/degradation) tracked separately under its own issue. **Fix round (adversarial review, #1374, BLOCKING items A/B/1 plus 3/9/nit):** (A) `agent_blueprints.py`/`routes/blueprints.py` ratchets reverted to 1056/877 by moving the floor-activation seam into the new `gact/blueprint_activation.py::agent_blueprint_activation_metadata` and the install-row/checksum logic into `gact/agent_blueprint_refresh.py::install_row`; both files hold at 1055/877. (B) `A2UIStore` folded every session's full part history on EVERY write/read (`project_a2ui_parts`/`fold_action_records` from empty state each call, O(n) per call, O(n^2) total; an action POST cost 2 full refolds; the 260-action bounds test measured ~475s originally, ~116s once a separate `sessions_path=None`-is-not-in-memory pollution bug in the test harness was also fixed). Fixed with a per-session `_ProjectionCache` (self-verifying: reusable only when the registry generation, the session's active-blueprint identity, AND both part-id sequences are pure extensions of the cached ones — any other change forces exactly one full refold, proven by dedicated call-counting tests with sabotage verification) plus incremental fold support added to `project_a2ui_parts`/`fold_action_records`; `test_a2ui_load_bounds.py` dropped from 135.95s to 19.44s after ALSO fixing the disk-flush pollution bug. (1) `requires.clio_agent` now also gates `install_agent_blueprint` and `discover_agent_blueprints`/listing (previously only session activation was gated — install-route floor enforcement was ABSENT before this fix round); a malformed specifier is now a typed `blueprint_requires_unparseable` validation error instead of silently treated as satisfied; a blueprint-level `requires` no longer duplicates onto every expert row (`validate_agent_blueprint_path` now filters row-inherited errors already present at blueprint level). (3) Re-installing a pack from a changed source now reports `replaced: {previous_checksum, checksum}` on the install route and records the same typed `source_checksum_changed` reason the boot-time refresh path already used, both paths tested. (9) `test_a2ui_desktop_parity.py` now asserts against vendored, provenance-tagged fixtures (`tests/fixtures/a2ui_client_metadata/`, derived from the gact-tui `web/e2e/a2ui-fixtures.mjs`/`desktop/tests/` Tauri-origin recordings) instead of hand-written headers. (nit) an out-of-turn (`turn_id=""`) producer refusal is never counted as "the same turn, recurring"; `DELETE /v1/sessions/{sid}` now prunes `CatalogRegistry`'s and `A2UIStore`'s per-session state via new `forget_session` methods. **Focused re-review (adversarial re-review of `779255ec`/`0c294275`, #1374, BLOCKING items 1/2/HIGH plus 3-6):** (1, BLOCKING) the incremental fold's action pass could leave a repair-exhausted `error` stale on the wire after a LATER successful update (a from-scratch fold's action pass runs once, at the END, against the final revision; the incremental fold ran the a2ui and action passes in separate calls) — fixed by clearing `error` on every successfully-staged message (`a2ui.py::_apply_staged_message`), re-set by `fold_action_records` only when a fresh exhausted record at the NEW revision still applies. (2, HIGH) a `createSurface` recreating a previously-deleted id built a brand-new record with empty `actions`, silently detaching that id's session-scoped action history (and making the action idempotency check cache-order-dependent) until a NEW action part happened to arrive — fixed with `created_surface_ids`/`reattach_surface_ids` threaded from `_apply_staged_message` through `project_a2ui_parts`/`fold_action_records` so a recreate always re-attaches history immediately, matching a from-scratch fold. (3) `A2UIStore`'s projection cache could outlive the #889 resident-ledger bound — `ResidentLedgerSet` gained an `on_evict` hook (`resident_ledgers.py`) wired to `A2UIStore.forget_session`, so a genuine capacity/idle-TTL eviction drops the projection cache entry too. (4) the blueprint-vs-row floor-error dedup (item 4 of the first fix round) only covered `validate_agent_blueprint_path`'s own aggregation; fixed at the true source (`expert_packs.py::parse_expert_file` no longer copies `pack.validation_errors` onto a row's errors/metadata at all), so `GET /v1/agent-blueprints/{id}` is fixed too. (5) the install route's refusal envelope carried only a generic `error: "validation_error"` with the typed code buried in prose; `install_agent_blueprint` now raises a typed `AgentBlueprintInstallRefused` (`gact/agent_blueprint_requires.py`) the route turns into `details.validation_errors` (list) + `details.codes` (machine-readable). (6) by-path session activation's own upstream refusal never reached the `blueprint.resolution.degraded` reason ledger by-id activation reaches (its own docstring's "identically defended" claim was true of the CHECK, not the ledger) — fixed via `record_floor_reason_if_declared`; and the install-reason ledger reached no API/trace by default (only a gated `stream_audit` call) — `record_blueprint_install_reason` now also calls `runtime.trace.event` unconditionally and emits a `blueprint.install.reason` semantic event when app context is available (threaded from the install route). New permanent differential test `test_a2ui_projection_equivalence.py` compares `A2UIStore.list_wire()` against an independent from-scratch fold after every step across 7 scenarios (plain updates, repair-exhaustion+update, delete+createSurface+action-repost, late `recorded_at`, catalog invalidate, `_replace_session_messages`, resident eviction); items 1 and 2 verified failing-first by sabotage. | [#1374](https://github.com/iowarp/clio-agent/issues/1374) | [#1384](https://github.com/iowarp/clio-agent/pull/1384) | feat/a2ui-s8 (24 commits, see PR) | `tests/fixtures/a2ui_transcripts/v0_9_2_workspace_surface.json`, `tests/fixtures/a2ui_packs/second/`, `tests/test_gact/test_a2ui_v092_replay.py`, `test_a2ui_catalog_uninstalled_replay.py`, `test_a2ui_two_blueprint_isolation.py`, `test_a2ui_durability.py`, `test_a2ui_load_bounds.py`, `test_a2ui_desktop_parity.py`, `gact/agent_blueprint_requires.py`, `gact/a2ui_producer/_refusal.py`, `gact/a2ui_catalogs/registry.py::record_producer_refusal_reason`, `gact/agent_blueprint_refresh.py::_reinstall_reason`, `tests/test_gact/test_agent_blueprints.py` (requires-floor + registry-sync-checksum tests), `docs/gact/a2ui-binding.md` Desktop parity section, `gact/blueprint_activation.py::agent_blueprint_activation_metadata`, `gact/agent_blueprint_refresh.py::install_row`/`record_blueprint_install_reason`, `gact/a2ui_store.py::_ProjectionCache`, `tests/test_gact/test_a2ui_projection_cache.py` (5/5), `tests/fixtures/a2ui_client_metadata/` (vendored, provenance-tagged), `tests/test_gact/test_a2ui_projection_equivalence.py` (7/7, permanent differential test), `gact/resident_ledgers.py::build_resident_ledger_set`'s `on_evict` hook, `gact/agent_blueprint_requires.py::AgentBlueprintInstallRefused`/`typed_error_codes`/`install_refusal_http_exception`/`path_activation_invalid_http_exception`/`record_floor_reason_if_declared` | implemented locally, PR pending |
| S9 | Conformance in CI, bounded claim, records | [#1363](https://github.com/iowarp/clio-agent/issues/1363) | gact-tui docs PR [#412](https://github.com/iowarp/gact-tui/pull/412) (merge `e519ff20`, develop); clio-agent docs PR: see this PR | feat/a2ui-s9 | clio-schemas 0.3.2 main @`d68c72c8` (PR [#11](https://github.com/iowarp/clio-schemas/pull/11)): official corpus `tests/a2ui_corpus/v0_9_1/` run by `tests/test_a2ui_corpus.py` in CI (`.github/workflows/ci.yml`). clio-agent develop @`d433b41f`: corpus `tests/fixtures/a2ui_corpus/v0_9_1/` run by `tests/test_gact/test_a2ui_catalog_registry.py::TestOfficialCorpusConformance` and `tests/test_gact/test_a2ui_corpus_conformance.py` under CI job `check (3.12\|3.13)`; pack composability `tests/test_gact/test_a2ui_pack_composability.py`. gact-tui develop @`3619a715`: vitest corpus `web/src/lib/a2ui/a2ui-corpus.test.tsx` (43 vendored examples) plus desktop corpus smoke (`desktop/tests/a2ui-corpus-smoke.test.mjs`, `pnpm --filter @clio/desktop test:corpus`) in CI job "lint · typecheck · test · build · visual", step "Desktop packaged-bundle A2UI corpus smoke"; native WebView and both Tauri debug builds green ("native WebView proof · fixture-backed", "tauri debug · ubuntu-22.04", "tauri debug · windows-latest"). See the Closure and Public claim sections below. | docs landed, PR pending |

Per-slice detail (files, deletions, tests, exit gates) is the plan of record; each slice
issue carries its full specification.

## Deletion inventory

| Item | Location | Slice |
|---|---|---|
| 30-model union, `A2UIComponent`, `trusted_component_names`, `A2UI_CATALOG_ID` | clio-schemas `a2ui_v091.py:22,491-528,566-569` | S1 |
| Closed 5-name action Literal | `a2ui_v091.py:535-541` | S1 |
| `CLIO_A2UI_CATALOG_ID` equality + record overwrite | `gact/a2ui.py:369,484`; `protocol/constants.py:9`; `protocol_v3.py:6,25` | S2 |
| `SERVER_ACTIONS`/`CLIENT_ACTIONS`, `required_context`, `_component_validation_error`, functionCall rejection | `gact/a2ui.py:87-90,196-218,336-344,285-286` | S2 |
| Docstring wall + kind labels; whole module | `gact/a2ui_tools.py` | S4 |
| `/lastAction` ack, submit helpers, `form.submit` echo, `context.text` requirement | `routes/a2ui.py:39-53,170-172,249-250,254-267` | S5 |
| `LOCAL_ACTIONS`, local branch, pre-validation, revision remount, label switch | `a2ui-surface.tsx:66,95-113,153-173,245-260,285` | S6 |
| `use-a2ui-local-actions.ts` (+test); `lib/a2ui-state.ts` reader | gact-tui `web/src` | S6 |
| Generated global zod/TS union | `packages/core/src/generated/clio-schemas/a2ui-component.schema.ts`, `a2_u_i_component.ts` | S6 |
| `form.submit` / `agent.submit` guidance | marketplace skills | S7 |

Every deleted vehicle keeps its feature: local actions → catalog functions (S6); prop lore →
catalog instructions + schema (S4); `/lastAction` → action records + events (S5); allowlist →
catalog file + sidecar (S2).

## 1.0 seam points

Envelope parser per version module; registry key `(catalogId, protocolVersion)` with catalog
`protocolVersion` absent ⇒ 0.9.1; validation entry point `(catalog_id, component)`;
capability namespace from the version module; action shape per version (`userMessage`
honoured by narration); new catalogs UAX#31-named, `clio-workspace/v1` frozen 0.9.1-only.

## Not in scope (stated in the claim, not deferred by issue)

- Pack-shipped renderer code for a component type CLIO's renderer does not implement. Path
  if ever needed: add the implementation to CLIO's kernel as a normal feature, or a future
  sandboxed-component mechanism shared with MCP Apps.
- A2UI 1.0 (seams only), inline catalogs, A2A transport, MCP-tool progressive disclosure.

## Public claim (S9, 2026-09-17)

"CLIO implements A2UI v0.9.1: the official message, capability, data-model and error schemas;
the official Basic catalog plus blueprint-declared, pack-installed catalogs; catalog functions
and checks; validated against the official conformance corpus in browser and desktop. Not
supported: A2UI 1.0, inline catalogs, A2A transport, pack-shipped renderer code."

Not supported:

- A2UI 1.0 (seams only, see "1.0 seam points" above)
- Inline catalogs (`acceptsInlineCatalogs` defaults false; a policy decision, not a protocol gap)
- A2A transport (CLIO's transport is GACT)
- Pack-shipped renderer code for a component type CLIO's renderer does not implement

### Live evidence

The claim's "in browser and desktop" clause is proven by two independent kinds of evidence,
never conflated:

- **Browser/Tauri library path**: proven by CI, not a live run. clio-schemas' official corpus
  (`tests/a2ui_corpus/v0_9_1/` via `tests/test_a2ui_corpus.py`) and clio-agent's vendored copy
  (`tests/fixtures/a2ui_corpus/v0_9_1/` via `tests/test_gact/test_a2ui_corpus_conformance.py`,
  CI job `check (3.12|3.13)`) validate every message server-side. gact-tui's vitest corpus
  (`web/src/lib/a2ui/a2ui-corpus.test.tsx`, 43 vendored examples) and the desktop packaged-bundle
  smoke (`desktop/tests/a2ui-corpus-smoke.test.mjs`, step "Desktop packaged-bundle A2UI corpus
  smoke" in CI job "lint · typecheck · test · build · visual") render the same corpus client-side;
  the desktop smoke additionally asserts no unexpected console or page errors while the 43
  examples render (the vitest corpus asserts rendering only). On gact-tui PR #411 (merge `3619a715`), the native WebView proof
  ("native WebView proof · fixture-backed") and both Tauri debug builds ("tauri debug ·
  ubuntu-22.04", "tauri debug · windows-latest") also ran green.
- **Server/agent path**: proven by a real live harness, not CI. `tests/test_real_cases/test_earthscope_interactive.py`
  (S7's harness) ran live on 2026-09-17 against the EarthScope pack catalog with `claude_code`/
  `sonnet`: idle delivery PASS (surface staged on the pack catalog, a human selected two stations,
  the resumed turn staged exactly those two files) and queued delivery PASS (a mid-turn steer
  consumed exactly once). `codex`/`gpt-5.5` idle delivery also PASSED, with declared narration
  (S5b). The waiting-user cell is recorded as model-dependent: the model under test treats the
  rendered surface as the answer to its own question and ends the turn rather than issuing a new
  one, so the model side of that cell was not observed live; the resume correlation itself
  (`ask_user`'s `surface_id`, `user_question_resume.py` forwarding `question.answer_metadata`) is
  proven in-process by `tests/test_gact/test_a2ui_actions.py`, not by this live run.
- **S9 re-run on merged develop** (`d433b41f`, 2026-09-17, `claude_code`/`sonnet`, same idle
  scene): PASS in 743 s on the third attempt. Attempts 1 and 2 (1437 s and 1246 s) reached the
  same A2UI phases (surface `ready` on the pack catalog, station ids extracted, the
  `earthscope.stations.selected` action accepted) and then the harness-spawned clio-core daemon
  crashed during the resumed turn (`event_manager_win.cc` `WSAEventSelect ADD failed`, exit
  `0xC0000409`, session `error`, no-progress timeout). Attribution was tested, not assumed: a
  controlled A/B on pre-S8 develop (`85d3b26c`) passed in 766 s in the same hour, and a
  counter-probe of the S8 server diff replaying the scene found no change in clio-core/ARC
  reach on this path (S8's only new ARC-reaching code, the install-reason event, never fires
  in the scene). Two crashes followed by a pass on the same tree makes this the intermittent
  Windows daemon crash class already tracked on clio-agent #1242, not an A2UI or S8 defect;
  the evidence is recorded there. The live cell is therefore PASS with that caveat stated.

The marketplace pack itself (draft PR [#70](https://github.com/iowarp/clio-agent-marketplace/pull/70)
on branch `feat/a2ui-compat` @ `e9f6bef`, CI green) is not yet mergeable: it declares
`requires: {clio_agent: ">=0.9.4.15"}` (lowered from `>=0.9.5` by owner ruling 2026-09-23); clio-agent 0.9.4.15 is the first release that satisfies it.
