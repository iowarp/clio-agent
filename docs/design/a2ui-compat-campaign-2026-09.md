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
| S6 | Client runtime: registry, incremental processor, functions, capabilities, errors, lifecycle. Owes: kernel media/artifact components enforce the URL scheme allowlist on bound/resolved values at render time and report VALIDATION_FAILED (owner decision 11 — the server only checks literals as of S2). | | | | | |
| S7 | EarthScope pack catalog: composability proof + live gate | | | | | |
| S8 | Hardening: library 0.11.x, replay/degradation/parity | | | | | |
| S9 | Conformance in CI, bounded claim, records | | | | | |

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

## Claim (published only when S9's ledger has evidence for every row)

"CLIO implements A2UI v0.9.1: official message, capability, data-model and error schemas; the
official Basic catalog plus blueprint-declared, pack-installed catalogs; catalog functions and
checks; validated against the official conformance corpus in browser and desktop. Not
supported: A2UI 1.0, inline catalogs, A2A transport, pack-shipped renderer code."
