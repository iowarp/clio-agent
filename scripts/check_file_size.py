#!/usr/bin/env python3
"""Ratchet guard against god-files in the clio_agent source tree.

This check exists to prevent re-accretion of monolithic modules now that the
gact decomposition (iowarp/clio-agent#714, #767) has landed. It walks
``src/clio_agent/**/*.py`` and enforces a per-file line-count ratchet:

* A file **not** in :data:`RATCHET_BASELINE` may not exceed
  :data:`DEFAULT_MAX_LINES` -- a brand-new god-file fails the check.
* A file **in** :data:`RATCHET_BASELINE` (the known-oversized modules still
  awaiting decomposition) may not exceed its *recorded* line count -- it can
  shrink but never grow past where it is today.

The baseline may only ratchet DOWN (house precedent:
``check_silent_fallbacks.py::BASELINE_TOTAL``). When a file is brought under
the cap, or merely shrinks, the check reports the ratchet-down and the same PR
that shrank it updates :data:`RATCHET_BASELINE` (lowering the number, or
removing the entry once the file is under ``DEFAULT_MAX_LINES``). Ratchet-down
reports are advisory: they do not fail the build.

Run as part of CI (blocking) and locally::

    uv run python scripts/check_file_size.py
    uv run python scripts/check_file_size.py --max 600
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import NamedTuple

# Default maximum number of lines a single *non-baselined* source module may
# contain. New files must stay under this cap.
DEFAULT_MAX_LINES = 800

# Per-file ratchet baseline: the known-oversized modules at their current line
# counts, recorded so they cannot regrow. These are the files awaiting further
# decomposition (iowarp/clio-agent#714, #767). This mapping may only ratchet
# DOWN -- when a file shrinks, lower its number here (or drop the entry once it
# falls under DEFAULT_MAX_LINES) in the same change. Paths are relative to the
# repository root and use forward slashes.
RATCHET_BASELINE: dict[str, int] = {
    # #948 S4b: ClioAgent.agent.py dropped from its 2798-line baseline to ~723
    # once the dead Tier-1 planner half was deleted (host-only surface). Went
    # back under DEFAULT_MAX_LINES at that point, so its entry was removed.
    # #1232 pts 1+2: back over the cap (723 -> 961) for the boot-design fixes
    # whose per-workspace/per-blueprint decision genuinely belongs on
    # ClioAgent (the sole owner of the tool-gateway construction + the
    # workspace-executor registry these fixes extend):
    #   pt 1 (lazy blueprint-fleet mounting): _discover_pack_servers/
    #   _build_tool_gateway take a `blueprint_id` (mount ONE activated
    #   blueprint's servers, never every installed one) and
    #   _active_tool_executor evicts+rebuilds a workspace's resident executor
    #   when its active blueprint changes, computing that gateway's OWN
    #   preloaded-tool schemas via a bounded discovery pass so the blueprint's
    #   tools are actually visible to to_dspy_tools() (AsyncMCPToolExecutor
    #   freezes _mcp_tools at start() and never re-lists -- #932).
    #   pt 2 (non-blocking discovery): __init__ seeds builtins-only tool
    #   definitions synchronously and starts _start_mcp_namespace_discovery/
    #   _merge_discovered_tools on a background thread instead of blocking on
    #   the old serial list_tool_definitions pass; shutdown() stops the new
    #   healer thread symmetrically with the existing workspace reaper.
    # The bounded-concurrent pass + healer THEMSELVES live in the owner
    # module tools/mcp_discovery.py (no-accretion); only the ClioAgent-level
    # decision points (which blueprint/gateway, when to rebuild, how to merge
    # into the live catalog) land here, because they need ClioAgent's own
    # state (_tool_definitions, _tool_gateway, the workspace executor
    # registry). Ratchets back with the #714/#767 decomposition.
    # (957 -> 965: +8 to stop a stale NamespaceDiscoveryHealer thread leaking
    # on every periodic relay-catalog refresh -- request_stop() call + guard.
    # 965 -> 973: +8 for a `with suppress(AttributeError, TypeError)` guard
    # around the _clio_mounted_blueprint_id cache-bookkeeping stamp -- a
    # handful of pre-existing tests deliberately stub create_sync_tool_executor
    # with a bare string sentinel, which does not support attribute
    # assignment; production SyncMCPToolExecutor instances always do.)
    # +8 (b8eff254): the synchronous federation-projections seed at
    # construction (list_relay_tool_definitions merge -- the L3 run-4..9 fix).
    # +2 (c47441f6): mypy narrowing for the blueprint-switch eviction (an
    # assert + its comment); no behavior change.
    # #1230 (+6 on top): WorkspaceExecutorReaper.note_resolved() at the one
    # _active_tool_executor call site -- resolving-for-use now counts as
    # activity so a reap tick landing in the gap before the caller's dispatch
    # marks the executor busy cannot pop it out from under an about-to-start
    # call. The reaper's TTL pin itself lives in the owner module
    # tools/reaper.py; only the guarded one-call notify lands here.
    # +19 (#1236): federation-epoch eviction of resident workspace executors —
    # an executor minted while the federation was ABSENT must not outlive a
    # successful refresh (the run-15/17 custom_agent_tools_unavailable-with-
    # federation=present brick). Same eviction shape as blueprint_switched;
    # the epoch bump itself lives in gact/relay_wiring.py.
    # +18 (#1237 hotfix): blueprint activation's synchronous
    # discover_declared_tools_bounded() full-fleet pass at first resolve is
    # DELETED (owner ruling 2026-08-20: activation mounts nothing eagerly) —
    # replaced with a zero-I/O listing_cache read per declared namespace and
    # a stamped declared-namespace -> spec map (_clio_namespace_specs) that
    # the on-demand-mount seam (tools/mcp_discovery.ensure_namespace, called
    # from builders.py / mcp_executor.py) consults to tell "declared but not
    # yet listed" from "genuinely unknown". This call site is the sole owner
    # of "which blueprint/gateway, when to rebuild" (ClioAgent's own
    # workspace-executor registry); the on-demand mount machinery itself
    # lives in the owner module tools/mcp_discovery.py.
    # +5 (#1237 hotfix follow-on): stamp _clio_namespace_specs on the inner
    # AsyncMCPToolExecutor too (not just the sync wrapper), so
    # mcp_executor.py's _connect_namespace can gate its dispatch-time
    # launcher-cache-lock acquisition on the declared spec. (+5 more: that
    # stamp goes through getattr — the SyncToolExecutor protocol doesn't
    # declare _async_executor and test doubles lack it; mypy CI catch.)
    "src/clio_agent/agent.py": 1017,  # blueprint activation moved to gact/blueprint_activation.py
    "src/clio_agent/arc/memory.py": 1389,  # provider ladder moved to provenance_config.py
    "src/clio_agent/arc/segments.py": 1116,
    # #900: +4 for the CREATE_BREAKAWAY_FROM_JOB daemon-spawn flag + its rationale.
    # owner ruling 2026-07-14: +3 to route explicit =local through the loud
    # DEGRADED banner (owner module: arc/init_degradation.py).
    # #955 S7 adversarial-review closeout (886 -> 920): per-RPC stall granularity for
    # the multi-RPC put/clear (finding [3]) + the RPC-level health-probe wiring for
    # rpc_stalled quarantine recovery (blocker [1]). Both need the store's _cte/_client,
    # so they live here; the reusable guard helpers (guarded_store_rpc /
    # store_rpc_health_probe) were moved to the rpc_liveness owner module to hold the
    # growth down. Ratchet back below on the arc/storage decomposition (#714).
    "src/clio_agent/arc/storage.py": 896,
    # #737 S2 fold owner module. Crossed the 800 new-file cap restoring the FROZEN
    # arc.op reproducibility contract (§2 / GOAL.md DoD #4): the five working-set write
    # overrides now emit a per-op arc.op via _emit_op so arc.replay rebuilds the live
    # plane byte-identically (the S2 slice had dropped these, breaking replay). Footprint
    # minimized to concise docstrings; the per-op payload passing is irreducible. Ratchet
    # down with the #714/#767 decomposition.
    "src/clio_agent/arc/working_set_fold.py": 919,
    # 2026-08-04 (78f81d6f, unrelated to the P5 wire-semantics wave): +43 for
    # validate_agent_blueprint_path's new runtime_tool_names parameter -- pack
    # validation only knew builtins + pack mcp_servers namespaces, so an expert
    # declaring a serve-mounted relay tool (remote_*, relay_observe/relay_wait) was
    # disabled as "unknown tool reference" and took its whole root down by
    # hierarchy. The ratchet was not bumped in that commit; recorded here as
    # pre-existing CI-blocking debt this change closes (P5 adversarial review [A]).
    "src/clio_agent/gact/agent_blueprints.py": 1060,
    # #948 S4: +14 for the children-must-be-react hierarchy rule (a predict/CoT
    # parent would silently strand its children now that the settle loop routing
    # for it is deleted; typed validation error instead).
    # #948 S5: +7 to validate the dspy.BestOfN/Refine module variant declaration on the
    # row (the parse itself is the leaf runtime/type_parsing.parse_module_variant).
    "src/clio_agent/gact/expert_packs.py": 821,
    # #919: +35 to WIRE progressive-disclosure skills into all three module
    # classes (block + load_skill tool; logic lives in agents/skill_runtime.py)
    # and to document the deleted stale extract alias that crashed every
    # tool-user-agent build under ReActV2.
    # #952 S4 Pass C: -20 (the empty-answer settle/handoff-repair branches were
    # deleted; an empty blueprint/prompt-agent answer is now a typed failure).
    # #948 S4 live-gate fix: +29 for the child-scaled react iteration budget (the
    # declared-children resolution at the react build site + the scaling default).
    # #948 S5: +4 to wrap the inner program in the declared dspy.BestOfN/Refine module
    # variant at the dispatch (logic lives in the owner module agents/module_variants.py).
    # #953 [5]: +2 to carry the variant winner stamp (variant_selection) across the
    # BlueprintExpertModule.forward re-construction boundary (else silently dropped).
    # merge(main->develop): +43 (1833 -> 1876) integrating main's #962 external-MCP
    # permission-gate enforcement (_invoke_permission_gate / _external_mcp_permission_context)
    # + #964 sanitized-observer projection at the external MCP call site.
    # P1.4 #1066: +1 for the build_plan_exit_tool import; the two react-build call sites stay
    # line-neutral (the create_artifact append became a two-tool extend). All plan_exit logic lives
    # in the owner module gact/plan_mode.py. Ratchets back with the #714/#767 decomposition.
    # P0.1d (#1105): 1871 -> 1861 via behavior-neutral failure-event doc compaction.
    # Between that recording and this one, the tool-instrumentation/MCP-title
    # commits (15a009d0/1a57844a/a09986d6) carried the file to 1896 without a
    # matching baseline bump -- a pre-existing 35-line gap this change did not
    # introduce. Obs Tools tab "called | available" toggle: +27 net (1896 -> 1923)
    # for the ``agent.toolset.recorded`` wiring at the two instrument_tools() call
    # sites -- the registry + emit helper themselves live in the owner module
    # gact/agents/toolset_inventory.py (no-accretion ground rule); only the
    # per-call-site provenance registration (which sub-list a tool came from is
    # only known here) stayed in this file.
    # #1201 (adversarial review, PR #1202): +16 to surface a recorded MCP
    # connection-era downgrade into the session's semantic-event trace at
    # _active_base_agent_tool_executor (the natural per-session executor-
    # resolution reader site) -- the actual event-building logic lives in the
    # owner module gact/mcp_connection_observability.py; only the small
    # _emit_mcp_downgrade_events call-site wrapper + its two call sites land here.
    # +13 (b8eff254): the typed custom_agent_tools_unavailable diagnostics
    # block (fires only on the brick path; the L3 run-4..9 hunt).
    # +78 (#1237 hotfix): on-demand mounting at the expert-tool resolve seam
    # (owner ruling 2026-08-20: a declared tool's server mounts ON DEMAND,
    # not eagerly at activation) -- _resolve_declared_tools_with_on_demand_mount
    # and _mount_failure_reason are the decision logic for WHICH namespaces
    # need mounting and how their per-tool source/mount-failure reason is
    # named for _dynamic_agent_tools' existing brick/degrade branches. The
    # single-flight/liveness-driven mount PRIMITIVE itself lives in the owner
    # module tools/mcp_discovery.py (ensure_namespace); only the expert-
    # resolve-specific decision (which namespace, how to merge into THIS
    # executor, how to name the failure) belongs here.
    "src/clio_agent/gact/agents/builders.py": 2030,
    # #948 S4/S5/S6 growth already carried this file past the flat 800 cap (to 842)
    # before it was ever added to this baseline — a pre-existing gap this change
    # did not introduce (it was silently exempt from the ratchet, not under it).
    # P5 (wire semantics): +81 net for the fan-out group identity (spawn_group_id/
    # group_size threaded through _do_spawn/spawn_agents_parallel/the started+
    # completed expert_handoff Parts) and wait_agent_tasks's structured-content
    # declaration + per-task row building; the pure derivation logic (group-field
    # projection, structured-row/summary shaping) was extracted to the new owner
    # module agents/spawn_group.py to hold this file's growth down. Ratchet back
    # with the #714/#767 decomposition (a spawn_agent_task / wait_agent_tasks /
    # spawn_agents_parallel split is the natural next cut).
    # P5 adversarial review [E]: 923 -> 922 net -- the failed-sibling terminal Part
    # (group_size reconciliation on a refused batch spawn) + the fanout.started
    # group_size field, offset by trimming this file's own docstrings (the pure
    # metadata-row builder lives in the owner module spawn_group.py).
    # P5 (native tool declared output semantics): +11 -- check_agent_tasks now
    # declares its typed structured_content shape (message-first, wait_agent_tasks's
    # own treatment) at its one return site; the tally/format logic lives in the new
    # owner module agents/task_summary.py (shared with observe_agent_tasks), so only
    # the two-import + one-call declaration site landed here.
    "src/clio_agent/gact/agents/spawn_runtime.py": 933,
    # (invoker.py's entry retired 2026-08: RelayExpertInvoker moved to its own
    # owner module agents/relay_expert_invoker.py, dropping invoker.py under the
    # 800 default cap — the #1221/#1222 contract-alignment growth that broke the
    # 803 baseline lives there now.)
    # #900: +14 for the lifespan child-reaper install + clean-shutdown teardown wiring
    # (both delegate to the owner module runtime/process_tree.py).
    # #918: +7 for the SkillNotDelegatableError exception handler (app.py owns
    # the handler cluster; see _validation_exception_handler precedent).
    # #947 DEBT (recorded 2026-07-18, #948 S4): part of this count is inherited
    # MCP-apps landing growth (merged to develop with the size check red, baseline
    # 2545 -> actual); ratchet back below the pre-#947 count with the mcp_app_*
    # owner-module split (see the #947 DEBT block on mcp_apps.py).
    # #952 S4 Pass C: -9 (the dead delegation-helper re-export cluster was deleted
    # with the settle layer).
    # merge(main->develop): -3 ratchet down (2712 -> 2709).
    # #968 S2: +5 for the artifacts route registration (import + register call);
    # the route body lives in its own owner module routes/artifacts.py.
    # #971 (S5 boot-fold): +4 to wire the artifact-registry boot fold into agent
    # construction (import + call + wedged-store early-return), gating agent readiness
    # so the O(corpus) fold never lands on the tool hot path (defect 2) and a wedged ARC
    # store fails loud at boot, not mid-turn (defect 1b). The fold + typed-stall + the
    # boot_fold_artifact_registry_offloop helper all live in the owner module
    # artifacts/registry_boot.py (no accretion); only the call site is here.
    # #975 (B1 sandbox): +11 to wire the OS write-confinement backend at boot — the
    # install_sandbox() call in the lifespan + the emit_boot_state_event() call after
    # _set_app_arc. All logic (ladder, detection, doctor probe, boot-event emit) lives in
    # the owner module runtime/sandbox.py; only the two call sites are here (the #900
    # child-reaper precedent). Ratchets back with the #714 lifespan split.
    # #1035 (epic #1031 Pillar 2): +7 to wire the loop-inbox mid-turn wake carrier
    # into boot — the _make_loop_inbox_drain import, the per-session app.state
    # .loop_inboxes store (beside deferred_resumes), and the pending_loop_inbox_drain
    # hook install (mirrors pending_cancellation_checker). All logic lives in the
    # owner module gact/loop_inbox.py; only these boot call sites are here. Ratchets
    # back with the #714 lifespan split.
    # #1036 (epic #1031 Pillar 2): 2737 -> 2695 — deleted the deferred_resumes init +
    # _redrive_deferred_resume; the resume fold's re-drive now lives in the owner
    # module gact/loop_inbox.py (drain_inbox_to_new_turn), leaving only the idle-hook
    # wiring here.
    # P2.3 (#1071): +6 — the deferred-boot branch wires the tool_interceptor +
    # PostToolUse producers (make_post_tool_hook); all logic lives in the owner
    # module gact/hooks/intercept.py, only the two boot assignments are here.
    # P4.3 (#1081): 2679 -> 2552 — the scheduler tick + fire runtime
    # (_scheduler_tick/_scheduler_tick_once/_fire_schedule/_seconds_until_next_minute)
    # moved to the owner module gact/scheduler_runtime.py; app.py only re-exports them.
    # P0.1d (#1105): -1 removing the stale _jsonish re-export after wire-mode migration.
    # 2026-08-04 (78f81d6f, unrelated to the P5 wire-semantics wave): +7 for the
    # lifespan pre-import of litellm -- concurrent imports (deferred agent init vs
    # provider-bind construct_agent_with_relay) raced importlib to
    # KeyError('litellm'), turning every message POST into a 503. The ratchet was
    # not bumped in that commit; recorded here as pre-existing CI-blocking debt
    # this change closes (P5 adversarial review [A]).
    # #1205: +11 for the async-processes route registration (import + register call)
    # and the MCP-task event-publisher boot install (import + install call); both
    # bodies live in their own owner modules, routes/async_processes.py and
    # mcp_task_events.py.
    # #1211: +1 for the POST /v1/providers/models/refresh route registration (import
    # + one register call); the body lives entirely in its own owner modules,
    # routes/provider_models_refresh.py and providers/model_discovery.py.
    # #1232 pt 4: +10 to sequence the boot orphan-process reap BEFORE the
    # existing #1001 MCP-cache prune's peer-liveness check (a still-running
    # orphaned clio_run.exe from a prior hard kill otherwise looks like a live
    # peer and defers the prune indefinitely — the observed "deferred for two
    # days" bug). All reap logic lives in the owner module
    # runtime/process_census.py (reap_orphaned_processes/boot_reap_off_loop);
    # only the sequencing wrapper + its one call site land here.
    "src/clio_agent/gact/app.py": 2574,  # relay wiring moved to gact/relay_wiring.py; +6 one-line provenance_wiring calls (#1247)
    # #971 GAP A (S5 live gate): the artifact mint funnel was at the 800 cap; +24
    # adds the designation-by-RESULT channel (ndp_stage_resource writes an
    # intermediate whose path rides only ``local_path`` in the result — the arg
    # channel can't see it, so the downstream clean recorded external-with-sha
    # instead of a hash-pair edge). The combined single-loop over both channels is
    # the minimal footprint; ratchets back with the #714 mint/registry split.
    # #972 S6 review fixes: +6 for two owner-mandated features glued at the funnel —
    # the harness-write CAS ingest (finding [3]; its 26-line resolver was RELOCATED to
    # cas.harness_write_identity, not added here) and the single-site running-byte
    # counter bump (finding [6/7], one call at the mint funnel). Ratchets back with
    # the #714 mint/registry split.
    # #1191: +14 for the same-sha-dedup use/custody emit call in the mint funnel's
    # DEDUP branch (the honest "this session used the pre-existing version" fact —
    # a same-sha re-production previously left the deduping session's own artifact
    # surface with nothing to show for it). All logic (materialize + emit) lives in
    # the owner module artifacts/versions.py (emit_artifact_used); only the guarded
    # call site is here. Ratchets back with the #714 mint/registry split.
    "src/clio_agent/gact/artifacts/minting.py": 867,  # provider-store seam: funnel receipt stamp + per-site ingested threading (#1247)
    # #1191: not previously baselined (silently over the 800 cap already, from
    # earlier unbaselined growth on this branch — the create_artifact tool floor).
    # +19 net for the OPTIONAL used=[...] input-refs param on create_artifact (the
    # tool signature + desc/args schema); the resolver itself lives in the owner
    # module artifacts/declared_used_edges.py. Ratchets back with the #714
    # artifacts/proposals decomposition.
    # P5 (native tool declared output semantics): +5 -- promote_proposals's two
    # return points now declare create_artifact's typed structured_content shape
    # (message-first, wait_agent_tasks's own treatment) via ONE call each; the
    # summary-message composition + the declare call itself live in the owner
    # module artifacts/wire.py (create_artifact_summary_message /
    # declare_create_artifact_structured_content), so only the two-line
    # dict-then-declare-then-return conversion landed here.
    # A8/A9 (#1176): +28 for the create_artifact producer parity fix (tool/call_id
    # via proposal_effects._mint_producer) and the dedup-enrichment wiring
    # (ProposalOutcome.enrichment + the two promote_proposal dedup branches calling
    # proposal_effects._dedup_enrich). The decision/emit logic itself lives in the
    # owner modules artifacts/proposal_effects.py, artifacts/dedup_enrichment.py and
    # artifacts/versions.py (emit_artifact_enriched) — only the call sites + the
    # typed outcome field landed here. Ratchets back with the #714 decomposition.
    "src/clio_agent/gact/artifacts/proposals.py": 876,  # inline-content ingest channel through the selected store (#1247)
    # #971 GAP B (S5 live gate): not baselined before this entry (silently over the
    # 800 cap already, from earlier unbaselined growth on this branch).
    # #1191: +51 for the per-session artifact-USE index (record_artifact_used /
    # fold_artifact_used / used_artifact_ids_for_session + the ARTIFACT_USED_EVENT
    # fold wiring) — the honest "this session used the pre-existing version" fact a
    # same-sha dedup leaves unrecorded otherwise. Mirrors the existing
    # record_transform/fold_transform_recorded pattern on this same class. Ratchets
    # back with the #714 mint/registry split.
    # A9 (#1176): +46 for the dedup-enrichment fold wiring (ARTIFACT_ENRICHED_EVENT +
    # its _FOLD_EVENT_TYPES/fold_event_by_type entries, the _supplemental_annotations
    # index, and the three thin record_artifact_enrichment/fold_artifact_enriched/
    # supplemental_annotation methods) — mirrors the #1191 USE-index footprint above.
    # The actual decision logic was pushed OUT to the new owner module
    # artifacts/dedup_enrichment.py (not appended here) precisely to hold this down.
    "src/clio_agent/gact/artifacts/registry.py": 938,
    # #971 GAP B (S5 live gate): +51 for ``?include_children=true`` parent
    # aggregation — a parent orchestrator's own listing is empty while its spawned
    # children hold everything, so the flag unions descendant child-session
    # workspaces (resolved via the agent-task registry) and attributes each row.
    # #1191: baseline was already stale (865 recorded vs. 888 actual) from earlier
    # unbaselined growth on this branch before this entry; +31 more for the
    # OPTIONAL ?include_used=true param surfacing dedup-reused (not produced)
    # records in a separate `used` list. Ratchets back with the #714
    # routes/artifacts decomposition.
    # A9 (#1176): +18 for threading an ``Optional[ArtifactRegistry]`` param through
    # ``_version_wire``/``_record_wire``/``_record_wire_attributed`` + their call
    # sites — the session-scoped/workspace/by-name/by-id/pin routes ALL need the
    # registry in hand so a dedup-time SUPPLEMENTAL annotation (a version's own
    # ``annotation`` is immutable) merges into what the route actually SERVES. The
    # merge DECISION itself is a one-line call into the owner module
    # ``artifacts/dedup_enrichment.py`` (``merged_annotation``) — only the
    # threading landed here. Ratchets back with the #714 decomposition.
    "src/clio_agent/gact/routes/artifacts.py": 940,  # provider-owned serve rung (logic in artifacts/storage.py) (#1247)
    # #948 S4: +10 for round-tripping the module: declaration in the overlay
    # export (an exported react parent re-loaded as predict and failed the new
    # hierarchy validation).
    "src/clio_agent/gact/routes/agents.py": 931,
    # merge(main->develop): +2 blueprints, +63 catalog, +54 mcp integrating main's
    # #956 MCP-apps runtime tool exposure (runtime MCP tools surfaced in the GACT
    # catalog + reconnect/streamable-http route growth). Part of the #947 MCP-apps
    # decomposition debt; ratchets back with the mcp_app_* owner-module split.
    # #1192: +149 (861 -> 1010; the branch already carried an unbaselined +20 --
    # 881 actual before this change) for the two new file-explorer routes
    # (GET .../{id}/files + .../files/read). The bulk of the logic (listing walk,
    # traversal hardening, text/binary content typing, session-path-activation
    # resolution) lives in the new owner module gact/agent_blueprint_files.py; only
    # the two thin route handlers + their docstrings + the import block land here.
    # Ratchets back with the mcp_app_* / #714 route decomposition.
    "src/clio_agent/gact/routes/blueprints.py": 978,
    "src/clio_agent/gact/routes/catalog.py": 938,  # +4: /goal command dispatch wiring (#1080; logic in gact/goal.py)
    # #1201 (adversarial review, PR #1202): +6 for two direct-connect era-
    # classification call sites (call_external_mcp_tool + _external_mcp_inventory's
    # prompt/list branches) -- the classification logic itself lives in the owner
    # module tools/mcp_connection_era.py; only the server_id= threading + one
    # instrument_client_era() wrap for the bare-Client list branch land here.
    "src/clio_agent/gact/routes/mcp.py": 985,  # -14: handshake row shaping moved to routes/mcp_rows.py (#1111)
    # #947 DEBT (recorded 2026-07-18, #948 S4 branch): the MCP-apps landing grew
    # these files past their baselines without a ratchet update (it merged to
    # develop with the check job red). Recording current counts makes the debt
    # visible and blockable again; the MCP-apps owner-module decomposition in
    # flight (mcp_app_lifecycle/sandbox/runtime split) deletes these entries by
    # ratcheting each file back below its pre-#947 count. Do NOT grow further.
    # merge(main->develop): +15 (897 -> 912) integrating main's #964
    # call_tool_result_to_observer public-projection wrapper (delegates to the
    # tools/mcp_results.py owner module).
    # P0.1c (#1104): sandbox/CSP construction moved to mcp_app_sandbox.py.
    # P0.1d (#1105): 784 -> 770 after folding _wire_value into tools/mcp_runtime.py.
    # #1201 (adversarial review, PR #1202): +7 to surface a recorded MCP
    # connection-era downgrade into the session's semantic-event trace at
    # _bound_executor (the natural per-session executor-resolution reader
    # site for the MCP-Apps bridge); the event-building logic lives in the
    # owner module gact/mcp_connection_observability.py.
    "src/clio_agent/gact/mcp_apps.py": 777,
    # #895: +6 for threading the provider-generic thinking_level onto the LM bind
    # (LMProviderConfig arg + app.state.lm_config + the GET's thinking_level /
    # thinking_effective fields). The mapping logic itself lives in the owner
    # module providers/thinking.py, not here.
    # #1211 (ratchet DOWN, 1317 -> 1313): the GET /v1/providers/{id}/models handler
    # was refactored to overlay-first serving (providers/model_discovery.py owns the
    # overlay + api-key-resolution logic, deduped out of two inline copies here).
    # #1211 review D2/D5 (adversarial review, 1313 -> 1337): +24 for the
    # _default_model_for helper (the overlay-aware default_model consulted by both
    # the /v1/providers list row and the omitted-model bind path) and scoping
    # overlay-first GET serving to the CLI provider kinds only (HTTP-backed
    # providers keep their live handshake path unconditionally). Logic stays thin;
    # the discovery/overlay mechanics live entirely in providers/model_discovery/.
    # #1211 owner ruling 2026-08-14 (1337 -> 1343): +6 documenting
    # _default_model_for's claude_code cost-policy exception (the served
    # default deviates from "follows the CLI's own live default" for that one
    # provider) -- the policy ITSELF lives in providers/model_discovery/overlay.py's
    # record_refresh; this docstring update is the only change here.
    "src/clio_agent/gact/routes/providers.py": 1343,
    # #947 DEBT (recorded 2026-07-18, #948 S4): inherited MCP-apps landing growth
    # (merged to develop with the size check red, baseline 1478 -> actual); ratchet
    # back below the pre-#947 count with the mcp_app_* owner-module split (see the
    # #947 DEBT block on mcp_apps.py).
    # merge(main->develop): -2 ratchet down (1548 -> 1546).
    # B5 #979.2: +3 for the session-attach boundary emit seam (the emit logic lives in the
    # grants owner module; only the guarded one-call seam + its import land here).
    # #1034: +2 to pass approval_mode through create_session + patch_session (two arg-pass
    # lines; the axis logic lives in sessions.py + permission_gate.py, no accretion here).
    # #1036: 1551 -> 1545 — the ask-user resume fold replaced the inline deferred_resumes
    # stash with a one-call enqueue_user_steer + a hoisted resume_metadata (dedup).
    # P1.4 #1066: +14 for the plan-exit approval branch in the ask-user answer route (reuses the
    # UserQuestion surface, no new store); the mode transition + constraint-lift + resume live in the
    # owner module gact/plan_mode.py (resolve_plan_exit_answer). Ratchets back with the #714 split.
    # P2.3 (#1071): +10 — the PreCompact lifecycle hook fires at the compact route
    # before summarisation (thin dispatch_pre_compact call site; the event set lives
    # in the owner module gact/hooks/).
    # #1057 B2 review repair: +5 for the reserved-metadata guard at the /retry ingest
    # (typed rejection lives in gact/messaging.py; only the thin call site lands here).
    # #1080: +3 stop_session_goal cancel-both wiring (logic in gact/goal.py). Merged
    # baseline = actual post-merge count (both additions present).
    # spotter-ai (#1034 follow-on): +5 for the create/patch watcher arm/disarm hook
    # (a shared lazy import + a bare prior-mode fetch + two thin delegator calls; all
    # branching/spawn/cancel logic lives in the owner module gact/spotter_watcher.py).
    # #1215 S5: +10 (1577 -> 1587) for the session.create bring-up-timing seam
    # (an import + a one-line comment + start_phase/end_phase call sites
    # bracketing the create); the delta is larger than the 6-line hand-edit
    # because running the required `ruff format` on the touched file also fixed
    # pre-existing formatting drift already present at the base commit (verified:
    # the base commit's file already failed `ruff format --check`) -- no
    # additional logic, only canonical line-wrapping of untouched code. All
    # timer/registry logic lives in the owner module gact/runtime/bringup_timing.py.
    "src/clio_agent/gact/routes/sessions.py": 1587,
    # #1215 S5: crossed the 800 new-file cap (793 -> 809) for enrich_turn_context —
    # a thin timed combinator wrapping the TWO existing enrichment calls
    # (_enrich_with_context_files + _enrich_with_requested_memory_search) in ONE
    # "enrichment" bring-up phase, so the call site in turn.py stays a single
    # (actually net-negative-line) call instead of needing its own timing calls.
    # No logic moves; the two real functions are unchanged.
    "src/clio_agent/gact/enrichment.py": 809,
    # #933: +8 for the turn-scoped workspace-fleet lease in _tool_session_context.
    # #933 review hardening: typed workspace_lease_unavailable degrade when a
    # rooted turn has no leasable agent (+9).
    # #948 S4 live-gate fix: +22 for _BlueprintRootDisabled (typed disabled-root
    # failure; lives with its sibling turn exceptions).
    # P0.1d (#1105): 977 -> 960 after folding _jsonish into tools/mcp_runtime.py.
    # #1232 pt 1: +18 for tool_blueprint_context wiring into _tool_session_context
    # (resolving the session's active blueprint id + binding it alongside the
    # existing workspace-root binding for the turn) so
    # ClioAgent._active_tool_executor can mount exactly the activated
    # blueprint's declared servers. The blueprint-id contextvar itself lives
    # in the owner module tools/execution.py; only the per-turn resolve +
    # bind call site lands here (mirrors the existing tool_workspace_context
    # wiring immediately above it).
    # +6 (#1237 hotfix): _UnsupportedSessionAgent carries an optional
    # mount_failures map (namespace -> typed reason) so the exception itself
    # can name a declared tool's server + reason -- turn.py's except handler
    # is the only reader; the mount decision lives in gact/agents/builders.py.
    "src/clio_agent/gact/runtime/globals.py": 986,  # blueprint-path arg threading (#1247)
    "src/clio_agent/gact/streaming.py": 995,
    # #948 S5: +2 to read the RUN-KEYED tap-dedup bucket under an in-process module
    # variant (context.run_keyed_scope; bare invoking_expert still owns attribution).
    # merge(main->develop): +10 (932 -> 942) integrating main's #964 structured
    # MCP-result preservation in the tool observer.
    # #966 S1 (artifacts seam a): +9 for the mint call site in the observer's
    # "completed" phase — the mint funnel + id/workspace resolution live in the
    # artifacts owner module; only the guarded one-call seam lands here.
    # B5 #979.7 (deferred B4 WRITER): +6 for the serving-child join seam in the
    # "started" phase — the join logic lives in the ingest_edges owner module
    # (join_call_to_serving_child); only the import + guarded one-call seam land here.
    # P2.3 (#1071): +7 — _install_tool_runtime_hooks defaults in the tool_interceptor
    # + PostToolUse producers; both producers live in the owner module
    # gact/hooks/intercept.py, only the thin default-in wiring is here.
    # #1190 + the collector-collapse work already on this branch grew the file to
    # 1049 (>the recorded 964 baseline) before this entry was updated — a pre-
    # existing gap this change did not introduce. P5 (wire semantics): +25 on top
    # of that for the ONE new integration point wait_agent_tasks needs — the
    # declared-structured-content pop/prefer in the "completed" phase and the
    # waited_tasks registry resolution in the "started" phase (both call OUT to
    # owner modules — tool_instrumentation.py / agent_tasks.py — for the actual
    # logic; only the two call sites land here). Ratchet back with #714/#767.
    # #1188 (MCP content-block half): +2 for the ONE new field this call site
    # wires — a top-level import + the inline ``content_blocks=`` argument on
    # the tool_result Part construction. The actual bounding/elision/lookup
    # logic lives in the owner module (tools/mcp_results.py::content_blocks_
    # for_wire); this is the theoretical floor for wiring a new Part field.
    # spotter-ai push-wake (owner design, no timers): +3 for the
    # wake_on_parent_activity call site right after the tool.call.completed
    # publish (a lazy import + one call). All gating/coalesce/wake logic lives
    # in the owner module gact/spotter_watcher.py.
    "src/clio_agent/gact/tool_observer.py": 1079,
    # Collector-collapse work already on this branch grew the file to 1303 (>the
    # recorded 986 baseline) before this entry was updated — pre-existing, not
    # introduced here. P5 (wire semantics): +34 for the waited_tasks union-merge
    # in upsert_repeated_collector_call's tool_call branch (a collapsed re-poll on
    # the same task set must never present fewer resolved rows than either
    # attempt saw). D15: +43 for discard_open_text — the fix for the duplicated-
    # narration wire defect (a transient LM retry re-streamed the SAME
    # next_thought text into the still-open transcript part). It is a genuinely
    # new TurnTranscript primitive tightly coupled to _lock/_open_part/_parts/
    # _buffers (no clean extraction to a sibling module without breaking
    # encapsulation), trimmed from an initial 77-line addition to 43 by cutting
    # the docstring and collapsing the log call before accepting this ratchet.
    # Ratchet back with #714/#767.
    "src/clio_agent/gact/transcript.py": 1380,
    # #918: +17 for the typed SkillNotDelegatableError ladder arm (a skill-bound
    # turn fails typed, never as generic agent_error).
    # #952 S4 Pass C: -1 (the suppressed_parent_resume_offsets init was removed
    # with the dead parent-resume duplicate suppressor).
    # #948 S4 live-gate fix: +27 for the blueprint_root_disabled catch arm (typed
    # error envelope with the root's validation errors; the except-ladder is
    # owned here).
    # #948 S6 [1]/[4]: +10 for the observe-later commit-to-run seam — staging the
    # injected task ids at enrichment + consuming/emitting each delegation terminal
    # immediately before forward dispatch (the fix for compose-time consumption +
    # dangling delegations). Load-bearing turn-orchestration wiring, comments minimized.
    # #966 S1 (artifacts seam c): +26 for the pack-declared artifact_paths finalize
    # seam — the secondary/optional designation channel mints declared output paths at
    # turn finalize. The mint funnel lives in the artifacts owner package; only the
    # guarded finalize helper + its one call site land here (never load-bearing).
    # P1.2 #1064: +5 for the plan-mode reminder enrichment call site (import + one-line comment
    # + the 3-line call). All logic (full/sparse selection, compaction detection, the
    # session.metadata suppression counter) lives in the owner module gact/plan_mode.py; only
    # the call site lands here (the #1035/#966 call-site precedent). Ratchets back with #714.
    # P1.4 #1066: +7 for the plan_exit turn-ending-yield seam next to maybe_pause_for_user (import +
    # comment + the 2-line call). The seam (maybe_pause_for_plan_exit) lives in the owner module
    # gact/plan_mode.py. Ratchets back with #714.
    # P1.6d #1068: -1 (839 -> 838) — the plan/todo/replan enrichment injects share one comment and a
    # verbose #6/#767 streaming comment was condensed; the inject_replan_suggestion call site (+4) is
    # more than offset. The stall-monitor + suggestion logic lives in the owner module replanning.py.
    # #1215 S5: +5 (838 -> 843) net for the turn.accept_gap bring-up phase (an
    # import + a one-line comment + start_phase/end_phase call sites), OFFSET by
    # collapsing the two separate _enrich_with_context_files/
    # _enrich_with_requested_memory_search calls into ONE enrich_turn_context call
    # (owner module gact/enrichment.py now times both as a single phase). No
    # enrichment logic moved here; only the call-site shape changed.
    # #1215 S5 follow-on: +4 (843 -> 847) for the finish_bringup() call site right
    # after forward_turn() returns (settles bring-up as far as instrumented on the
    # success path) -- import already added above, so just the comment + call.
    # #1215 S5 ruff-format pass: +8 (847 -> 855). Running the required `ruff
    # format` on the touched file also fixed pre-existing formatting drift
    # (verified: the base commit already failed `ruff format --check` on several
    # over-100-char lines this change never touched, e.g. the two
    # _context_file_turn_provenance calls) -- no additional logic, only
    # canonical line-wrapping.
    # +13 (#1237 hotfix): the _UnsupportedSessionAgent except handler names
    # the server + typed reason in the user-facing message/details when the
    # unavailability came from a failed on-demand mount attempt; the
    # mount_failures map itself is built in gact/agents/builders.py.
    "src/clio_agent/gact/turn.py": 868,
    # #952 S4 Pass C: -9 (the answer-substitution finalize call + import were
    # removed with the settle layer's degradation ledger).
    # #953 [5]: +3 to surface the variant winner stamp (variant_selection) on the
    # assistant-message metadata (additive observability contract).
    # #953 (workflow_state finalize seam): +11 to stamp the turn's produced typed
    # workflow_state onto the assistant message metadata (the root fix for a live-gate
    # miss where a chain_of_thought LEAF child's Prediction field never reached its
    # AgentTask result). The substantive merge lives in the owner module
    # (delegation._produced_turn_workflow_state); only the trivial import + stamp call
    # land here.
    # #968 S2: +12 for the resource_link finalize seam (item 2) — the append logic
    # lives in the owner module artifacts/wire.py (append_turn_resource_links); only
    # the import + one-line call land here. (A 34-line inline helper was moved out
    # and the dead settle-path clear removed to keep this to the minimum.)
    # #968 S2 review: -3 (946 -> 943) — the artifact.proposed payload dict moved to
    # the owner module (artifacts/wire.proposed_diff_payload, finding [2]); the
    # settle-path buffer clear delegates to artifacts/minting.clear_turn_artifacts.
    # P2.3 (#1071): +14 — PostToolBatch fires once per turn over the turn's tool
    # round (thin fire_post_tool_batch call site; the payload build + dispatch live
    # in the owner module gact/hooks/intercept.py).
    # spotter-ai push-wake (owner design, no timers): +3 for the
    # on_turn_finalized call site right after the session.status_changed
    # publish, alongside the existing dispatch_*_at_finalize hooks it mirrors
    # (a lazy import + one call). Logic lives in gact/spotter_watcher.py.
    "src/clio_agent/gact/turn_finalize.py": 978,  # +36 (A4 #1057): the loop-goal compose glue is extracted into the named, tested `compose_goal_loop_stop_at_finalize` seam (A4 review: the inline glue was silently deletable — the extracted function is driven by the finalize seam test); the glue owns the goal->loop import so goal.py stays a leaf (no cycle)
    # P5 (owner ask 2026-08-06): +7 for the child/subagent artifact-rollup call
    # site (comment + function-local import + one-line invocation, matching the
    # P4.1/P4.2/P1.6d dispatch idiom already used lower in this file); the
    # aggregation logic itself lives in the owner module
    # artifacts/wire.append_turn_child_resource_links (no-accretion).
    # #947 DEBT (recorded 2026-07-18, #948 S4): inherited MCP-apps landing growth
    # (baseline 1143 -> actual); ratchet back below the pre-#947 count with the
    # mcp_app_* owner-module split (see the #947 DEBT block on mcp_apps.py).
    # #968 S2: +11 for the resource_link Part fields (uri/name/server_id) + the
    # x_clio_artifacts capability flag — Part + CapabilityFlags were defined here
    # before the P0.1a #1102 owner-module extraction.
    # #1034: +5 for the approval_mode axis on the three session wire models (Session +
    # Create/Update requests) + a 3-line doc comment; the enum + enforcement live in
    # sessions.py + permission_gate.py, so only the field declarations land here.
    # P0.1a (#1102): move Part + CapabilityFlags to gact/parts.py; 1170 -> 958 lines.
    "src/clio_agent/gact/types.py": 958,
    # -120 (#891): the SDK-session machinery moved out to sibling owner modules —
    # the blocking-path pool to providers/claude_code_sdk_pool.py and the per-expert
    # streaming session/delta transport to providers/claude_code_sessions.py; this
    # file keeps only the LiteLLM handler + exec/stream plumbing. Ratchet back down
    # further with the #714/#767 decomposition.
    # -12: the thinking-channel emission seams (provider-thinking forward + the
    # provider_thinking_redacted typed reason) moved to their owner module,
    # providers/claude_code_thinking_split.py.
    # #1184 / #1211 review A3 (835 -> 861): +26 to classify a definitive
    # model-rejection (api_error_status==404) on the streaming ResultMessage path
    # and raise the shared typed litellm.BadRequestError (raise_model_rejected in
    # _cli_provider.py) instead of a bare ClaudeCodeExecError -- so the account's
    # rejection reaches the trace/transcript honestly instead of a misleading
    # LMTransportError, and is never retried as transient.
    "src/clio_agent/providers/claude_code_litellm.py": 861,
    # #900: +2 for wiring probe_process_tree into the doctor collect().
    # owner ruling 2026-07-14: +3 for the DEGRADED-by-policy local-ARC doctor row.
    # #947 DEBT (recorded 2026-07-18, #948 S4): residual over the pre-#947 count
    # (1188) is inherited MCP-apps landing growth; ratchet down with the mcp_app_*
    # owner-module split (see the #947 DEBT block on mcp_apps.py).
    # #985 move 1 (2026-07-19): +33 for config-first resolution of paths.data_dir /
    # runtime.api_base — the injectable ConfigStore, a shared _source_label helper for
    # the config/env/default provenance, and two thin probe seams (the conf.resolve
    # calls stay inline so the env-reference generator discovers each knob directly).
    # Real new functionality (env-only → config-first); ratchets down when the doctor's
    # probe methods are extracted to an owner module.
    # #975 (B1 sandbox): +2 to register the `sandbox` doctor row in collect() (the import
    # + the probe_sandbox() call); the probe logic lives in the owner module
    # runtime/sandbox.py. Ratchets down when the probe methods are extracted.
    # #1201 (adversarial review, PR #1202): +4 to register the mcp_yaml doctor
    # sub-check (import + the probe_mcp_yaml_declarations() call); the probe
    # logic lives in the owner module runtime/mcp_launcher.py.
    "src/clio_agent/runtime/status.py": 1244,
    # #932: +62 for preloaded tool definitions (start() without the list_tools
    # fan-out) and namespace-direct call routing with lazy per-namespace
    # clients — the executor IS the owner module for this.
    # #933: +23 for the reaper instrumentation: inflight refcount + idle clock,
    # plus the busy/idle_for accessors the reaper's drain guard reads (their
    # state lives on the executor, so the accessors are owned here too).
    # #934: +22 for the spawn-diet first-call hooks (the namespace backend
    # spawns on its first FORWARDED CALL, not ctx-enter, so the learn /
    # drop-plan-on-failure signals wrap the first routed call per namespace;
    # incl. the timeout-vs-connect-health caveat comment).
    # #947 DEBT (recorded 2026-07-18, #948 S4): residual over the pre-#947 count
    # (1304) beyond the #932/#933/#934 deltas is inherited MCP-apps landing growth;
    # ratchet down with the mcp_app_* owner-module split (see the #947 block on mcp_apps.py).
    # merge(main->develop): +257 (1490 -> 1747) integrating main's #964 sanitized
    # dual-projection (_MCPCallOutcome) + #965 mutating-tool timeout budget /
    # uncertain-timeout handling. Part of the #947 MCP-apps decomposition debt.
    # #966 S1 (artifacts): -109 (1747 -> 1638) — the grounding-hook constants +
    # _ground_output_paths moved to the artifacts designation owner module; only a
    # thin re-export wrapper remains here (deletion inventory item 2).
    # P1.2 #1064: +4 at the tool-gate denial to surface a mode-aware ``deny_message`` (a str
    # subclass carrying the plan-mode text) instead of the generic string — a getattr read + a
    # 3-line rationale. Irreducible: execution.py OWNS the PermissionError the model sees, and the
    # low tools layer imports no gact (duck-typed). The message text is produced in
    # grant_resolver.plan_mode_deny_message. Ratchets back with the #714/#767 decomposition.
    # P2.3 (#1071): +16 — the tool boundary now consults the tool_interceptor
    # (synthesize/modify) and applies the PostToolUse hook. The seam TYPES + the
    # applier live in the new owner module tools/tool_hooks.py; only the thin
    # ToolRuntimeHooks.post_tool field + the two call sites are here.
    # P0.1b (#1103): -500 — AsyncMCPToolExecutor, its client protocol/factory,
    # timeout/uncertain-mutation support, and MCP projections moved to mcp_executor.py.
    # Default-on tool instrumentation (owner 2026-08-05): +10 — TOOL_OBSERVED_ATTR
    # (the observed-callable marker MUST live in this low tools layer: the bridge
    # marks its own constructions and gact may import tools, never the reverse) +
    # the one-line stamp in _make_dspy_tool. All other logic lives in the owner
    # module gact/agents/tool_instrumentation.py.
    # #1188 MCP half: +4 — a lazy import + one-line call to the new owner-module
    # helper stamp_mcp_tool_title (gact/agents/tool_instrumentation.py), which
    # carries the upstream MCP tool's declared title onto Part.tool_title. The
    # substantive logic (sanitize + stamp) lives in the owner module; this file
    # only wires the boundary call.
    # #1201 (adversarial review, PR #1202): +6 for a server_id threading-through
    # parameter on create_async_tool_executor/create_sync_tool_executor/
    # SyncMCPToolExecutor.__init__ -- the classification/instrumentation logic
    # lives entirely in the owner module tools/mcp_connection_era.py; only the
    # parameter + its pass-through to AsyncMCPToolExecutor land here. +4 more
    # for a namespaces() delegate (the gact-side semantic-event readers need
    # to enumerate an executor's declared namespaces; the accessor itself
    # lives on AsyncMCPToolExecutor in mcp_executor.py, this is the thin
    # delegate).
    # #1232 pt 1: +35 for the tool_blueprint_context ContextVar + context
    # manager + get_active_tool_blueprint_id accessor -- the SAME pattern as
    # the existing tool_workspace_context immediately above it (this module
    # already owns that contextvar, so its sibling belongs here too, not a
    # new file). No behavior on the existing workspace-root path changes.
    # #1230: +5 (1214 -> 1219) — the unbounded #1225 commitment wait now scopes
    # inside `with commitment_activity.track(timeout is None):` so the turn's
    # no-progress watchdog can see it as progress and pause the ceiling. Logic
    # (the tracker + the no-op-unless-unbounded context manager) lives entirely
    # in the new owner module runtime/commitment_activity.py; only the import +
    # the one `with` line land here.
    # +11 (#1237 hotfix): SyncMCPToolExecutor.merge_namespace_tools -- the
    # thin sync delegate to AsyncMCPToolExecutor.merge_namespace_tools
    # (mcp_executor.py), the actual live-tool-table merge target for an
    # on-demand mount (gact/agents/builders.py).
    "src/clio_agent/tools/execution.py": 1245,  # _ACTIVE_TOOL_BLUEPRINT_PATH contextvar trio - this IS the owner module (#1247)
    # #1201 (adversarial review, PR #1202): not previously baselined (under the
    # 800 default cap). +24 for the unreadable-mcp.yaml snapshot (a reset-per-
    # call list + lock, mirroring the existing per-server MCPServerSpec.
    # validation_errors idiom already in this file) so the malformed-file
    # warning is queryable by the new doctor sub-check
    # (runtime/mcp_launcher.py::probe_mcp_yaml_declarations), not just a log
    # line. Ratchets back below 800 if this snapshot moves to its own module.
    # #1230: not previously baselined (sat exactly at the 800 default cap). +8
    # for the per-call ceiling derivation call site in _timeout_budget_for_call
    # (a component-declared schema-default budget now floors the backstop
    # above the flat operator-tuned global) — the derivation itself lives in
    # the new owner module tools/mcp_timeout_budget.py; only the import + the
    # property-extraction/compare lines land here.
    # +17 (#1237 hotfix): merge_namespace_tools -- the #932 _mcp_tools freeze
    # becomes append-only-mergeable so an on-demand mount (#1237, triggered
    # from gact/agents/builders.py) reaches to_dspy_tools() for THIS SAME
    # live executor instance instead of only a future rebuild.
    # +22 (#1237 hotfix follow-on): _connect_namespace -- the ACTUAL
    # dispatch-time cold spawn for a real tool call (a separate connection
    # from the discovery pass's own throwaway one) now goes through the SAME
    # liveness-driven launcher-cache lock discovery already used, gated by
    # the declared-spec map stamped by ClioAgent. Root-caused a real
    # thread/release mismatch bug in the async lock along the way (fixed in
    # the owner module tools/launcher_cache_lock.py: FileLock's default
    # thread_local=True silently orphans the OS lock when acquire/release
    # run on different threads, as they do here via asyncio.to_thread).
    "src/clio_agent/tools/mcp_executor.py": 847,
    "src/clio_agent/tools/mcp_config.py": 821,
    # #1231 Part 1/2 (consumer half of the live-console feature): not previously
    # baselined -- this file was ALREADY 7 lines over the 800 cap before this
    # change (unbaselined pre-existing debt), i.e. 807 -> 820. Part 1 (+6 net):
    # resolve TaskKey.session_id from the ACTIVE gact session at submit time
    # instead of freezing it to the relay owner-session id at __init__ (the
    # ``mcp_task_record_held_locally`` root cause) -- the resolution logic
    # itself lives in the owner module tools/relay_contract.py
    # (resolve_relay_task_session_id); only the two-field constructor storage +
    # the one-call submit-time resolve land here. Part 2 (+7 net): wire the
    # console-tail on_poll hook into ``wait()`` -- the pull/fold/config logic
    # lives entirely in the NEW owner module tools/relay_console.py
    # (make_console_on_poll); only the import + one keyword argument + a
    # docstring note land here. Ratchets back below 800 with the #714/#767
    # decomposition. +7 (820->827): the #1231 run-13 fix folds one console
    # increment on every explicit resolution in ``poll()`` (terminal-at-lookup
    # records and relay_observe peeks were console-less forever) -- the fold
    # logic itself stays in relay_console.py; only the hook call lands here.
    # +26 (827->853): the missing CLIENT half of #1231 -- register/unregister this
    # instance's console observer factory (tools/task_observers.py) against its own
    # backend_identity() server_id at __aenter__/__aexit__, so a relay-backed task
    # that resolves through the TRANSPARENT #1115 extension path (never through this
    # client's own submit()/poll()/wait()) also folds its console tail. The registry
    # + the guarded resolve-and-catch live in the new owner module
    # tools/task_observers.py; only the register/unregister call sites + the
    # _observer_server_id bookkeeping field land here.
    # +3 (853->856): ``backend_identity(getattr(mcp_client, "transport", mcp_client))``
    # -- a real ``fastmcp.Client`` always has ``.transport`` (matches submit()'s own
    # derivation exactly), but test_mcp_execution_era_visibility.py's minimal
    # era-classification fake client does not, and crashed __aenter__ before this
    # fallback (a pre-existing, unrelated test this change must not break).
    # +12 (856->868, #1236): ``poll()`` -- the explicit single-observation path --
    # now derives the SAME honest ``effective_status``/``effective_status_reason``
    # pair ``tools/mcp_tasks.py``'s own poll loop (``_record_status``) does, via
    # the shared ``derive_effective_status`` helper that OWNS the derivation logic
    # (mcp_tasks.py); only the one extra call + the two extra ``replace()`` kwargs
    # land here, so a task resolved through this explicit path (not the transparent
    # #1115 extension) does not read a delivered-error result as bare "completed"
    # either (clio-relay#265's "completed is a terrible status indicator" ruling).
    # +26 (868->894, clio-relay#221/#259 consumer half): the console-SSE
    # capability negotiation + reader-registry lifecycle call sites -- the
    # ``console_sse_supported`` field/accessor, the one ``probe_console_sse_
    # capability`` await at __aenter__, the ``_console_stream_registry`` field,
    # and its ``cancel_all()`` safety-net await at __aexit__ (before the http
    # client that owns the readers' connections closes). All SSE reading/
    # parsing/fallback logic lives in the new owner module
    # tools/relay_console_stream.py; only these thin construction/probe/cleanup
    # call sites land here, since this class is the sole owner of both doors.
    "src/clio_agent/tools/relay_transport.py": 894,
    # #1232 pt 2: not previously baselined (under the 800 cap). +28 for
    # list_builtin_tool_definitions -- the boot path needs a FAST,
    # synchronous, no-I/O tool-definitions seed (built-ins only) so
    # ClioAgent.__init__ never waits on any declared MCP namespace. The real
    # new logic (bounded-concurrent discovery + the background healer) lives
    # entirely in the owner module tools/mcp_discovery.py; this file only
    # gained the small builtins-only extraction (it needs fs_server/
    # shell_server/_list_tools_sync, already private to this module) plus a
    # docstring cross-reference on list_tool_definitions. Ratchets back below
    # 800 if this helper moves out too. (+~24 more, b8eff254: the federation
    # projections seed list_relay_tool_definitions -- the L3 run-4..9 fix.)
    "src/clio_agent/tools/gateway.py": 852,
    # #1001: doctor rendering + disk-GC surface moved to the ui/doctor.py owner module
    # (ratcheted 1156 -> 1135 in the same change).
    # merge(main->develop): +6 (1135 -> 1141) integrating main's release-stream cli deltas.
    # #977 (B3 sandbox): the `sandbox` verb dispatches to runtime/sandbox_cli.py and
    # run_doctor/--tune stub moved to their owner modules; the `--yes` flag (owner's
    # one-command install acceptance) adds it back slightly — net ratchet 1141 -> 1138 (still
    # a reduction vs the inherited baseline).
    "src/clio_agent/ui/cli.py": 1138,
}

# Root of the source tree to scan, relative to the repository root.
SRC_ROOT = "src/clio_agent"


class Failure(NamedTuple):
    """A file that breaks the ratchet (fails the check)."""

    rel: str
    count: int
    kind: str  # "new" (non-baselined over cap) or "regressed" (over recorded)
    limit: int  # the cap it broke (DEFAULT_MAX_LINES or the recorded baseline)


class RatchetDown(NamedTuple):
    """A baselined file that shrank -- advisory, not a failure."""

    rel: str
    count: int
    baseline: int
    under_cap: bool  # True once count <= max_lines (drop the entry entirely)


class Result(NamedTuple):
    """Outcome of a scan: failures fail the build, ratchet_downs are advisory."""

    failures: list[Failure]
    ratchet_downs: list[RatchetDown]


def _repo_root() -> Path:
    """Return the repository root (parent of the ``scripts`` directory)."""
    return Path(__file__).resolve().parent.parent


def _count_lines(path: Path) -> int:
    """Return the number of lines in ``path``."""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return sum(1 for _ in handle)


def check_file_size(
    scan_root: Path,
    *,
    rel_to: Path | None = None,
    max_lines: int = DEFAULT_MAX_LINES,
    baseline: dict[str, int] | None = None,
) -> Result:
    """Evaluate the per-file line-count ratchet under ``scan_root``.

    Args:
        scan_root: Directory tree to walk for ``*.py`` files.
        rel_to: Base directory used to compute the forward-slash relative path
            that keys into ``baseline``. Defaults to ``scan_root``.
        max_lines: Cap applied to files not present in ``baseline``.
        baseline: Per-file recorded line counts. Defaults to
            :data:`RATCHET_BASELINE`.

    Returns:
        A :class:`Result` splitting build-failing offenders from advisory
        ratchet-down reports.
    """
    if baseline is None:
        baseline = RATCHET_BASELINE
    base = rel_to if rel_to is not None else scan_root

    failures: list[Failure] = []
    ratchet_downs: list[RatchetDown] = []
    for path in sorted(scan_root.rglob("*.py")):
        rel = path.relative_to(base).as_posix()
        count = _count_lines(path)
        recorded = baseline.get(rel)
        if recorded is None:
            if count > max_lines:
                failures.append(Failure(rel, count, "new", max_lines))
            continue
        if count > recorded:
            failures.append(Failure(rel, count, "regressed", recorded))
        elif count < recorded:
            ratchet_downs.append(RatchetDown(rel, count, recorded, under_cap=count <= max_lines))
    return Result(failures=failures, ratchet_downs=ratchet_downs)


def _print_report(result: Result, max_lines: int) -> None:
    """Print the ratchet report (failures then advisory ratchet-downs)."""
    for entry in result.ratchet_downs:
        if entry.under_cap:
            print(
                f"OK (ratchet down): {entry.rel} is now {entry.count} lines "
                f"(<= {max_lines}) -- remove it from RATCHET_BASELINE in "
                "scripts/check_file_size.py."
            )
        else:
            print(
                f"OK (ratchet down): {entry.rel} shrank {entry.baseline} -> "
                f"{entry.count} -- lower its RATCHET_BASELINE entry to "
                f"{entry.count} in scripts/check_file_size.py."
            )

    if not result.failures:
        print(
            f"OK: no file under {SRC_ROOT} exceeds its ratchet baseline "
            f"(cap {max_lines} for new files)."
        )
        return

    print(f"FAIL: {len(result.failures)} file(s) break the size ratchet (#714, #774):")
    for entry in result.failures:
        if entry.kind == "new":
            print(f"  {entry.rel}:{entry.count} (new file exceeds cap {entry.limit})")
        else:
            print(f"  {entry.rel}:{entry.count} (regressed past recorded baseline {entry.limit})")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Return 0 if the ratchet holds, 1 on any failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max",
        type=int,
        default=DEFAULT_MAX_LINES,
        help=f"Cap for non-baselined files (default: {DEFAULT_MAX_LINES}).",
    )
    args = parser.parse_args(argv)

    repo_root = _repo_root()
    result = check_file_size(
        repo_root / SRC_ROOT,
        rel_to=repo_root,
        max_lines=args.max,
    )
    _print_report(result, args.max)
    return 1 if result.failures else 0


if __name__ == "__main__":
    sys.exit(main())
