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
    # once the dead Tier-1 planner half was deleted (host-only surface). Now
    # under DEFAULT_MAX_LINES, so its ratchet entry is removed entirely.
    "src/clio_agent/arc/memory.py": 1394,
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
    "src/clio_agent/gact/agent_blueprints.py": 1078,
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
    "src/clio_agent/gact/agents/builders.py": 1923,
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
    "src/clio_agent/gact/agents/spawn_runtime.py": 923,
    # P5 (wire semantics): +3 for the fan-out group identity fields
    # (spawn_group_id/group_size) on TaskHandle + TaskResult, projected from
    # AgentTask in from_task() and threaded through RelayExpertInvoker.invoke —
    # both dataclasses already carry ~15 parity fields each, so two more typed
    # fields (kept to single-line trailing comments) is the honest cost of one
    # more boundary-crossing fact, not a new logic cluster to decompose.
    "src/clio_agent/gact/agents/invoker.py": 803,
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
    "src/clio_agent/gact/app.py": 2544,  # relay wiring moved to gact/relay_wiring.py
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
    "src/clio_agent/gact/artifacts/minting.py": 856,
    # #1191: not previously baselined (silently over the 800 cap already, from
    # earlier unbaselined growth on this branch — the create_artifact tool floor).
    # +19 net for the OPTIONAL used=[...] input-refs param on create_artifact (the
    # tool signature + desc/args schema); the resolver itself lives in the owner
    # module artifacts/declared_used_edges.py. Ratchets back with the #714
    # artifacts/proposals decomposition.
    "src/clio_agent/gact/artifacts/proposals.py": 832,
    # #971 GAP B (S5 live gate): not baselined before this entry (silently over the
    # 800 cap already, from earlier unbaselined growth on this branch).
    # #1191: +51 for the per-session artifact-USE index (record_artifact_used /
    # fold_artifact_used / used_artifact_ids_for_session + the ARTIFACT_USED_EVENT
    # fold wiring) — the honest "this session used the pre-existing version" fact a
    # same-sha dedup leaves unrecorded otherwise. Mirrors the existing
    # record_transform/fold_transform_recorded pattern on this same class. Ratchets
    # back with the #714 mint/registry split.
    "src/clio_agent/gact/artifacts/registry.py": 892,
    # #971 GAP B (S5 live gate): +51 for ``?include_children=true`` parent
    # aggregation — a parent orchestrator's own listing is empty while its spawned
    # children hold everything, so the flag unions descendant child-session
    # workspaces (resolved via the agent-task registry) and attributes each row.
    # #1191: baseline was already stale (865 recorded vs. 888 actual) from earlier
    # unbaselined growth on this branch before this entry; +31 more for the
    # OPTIONAL ?include_used=true param surfacing dedup-reused (not produced)
    # records in a separate `used` list. Ratchets back with the #714
    # routes/artifacts decomposition.
    "src/clio_agent/gact/routes/artifacts.py": 919,
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
    "src/clio_agent/gact/routes/blueprints.py": 1010,
    "src/clio_agent/gact/routes/catalog.py": 938,  # +4: /goal command dispatch wiring (#1080; logic in gact/goal.py)
    "src/clio_agent/gact/routes/mcp.py": 979,  # -14: handshake row shaping moved to routes/mcp_rows.py (#1111)
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
    "src/clio_agent/gact/mcp_apps.py": 770,
    # #895: +6 for threading the provider-generic thinking_level onto the LM bind
    # (LMProviderConfig arg + app.state.lm_config + the GET's thinking_level /
    # thinking_effective fields). The mapping logic itself lives in the owner
    # module providers/thinking.py, not here.
    "src/clio_agent/gact/routes/providers.py": 1317,
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
    "src/clio_agent/gact/routes/sessions.py": 1572,
    # #933: +8 for the turn-scoped workspace-fleet lease in _tool_session_context.
    # #933 review hardening: typed workspace_lease_unavailable degrade when a
    # rooted turn has no leasable agent (+9).
    # #948 S4 live-gate fix: +22 for _BlueprintRootDisabled (typed disabled-root
    # failure; lives with its sibling turn exceptions).
    # P0.1d (#1105): 977 -> 960 after folding _jsonish into tools/mcp_runtime.py.
    "src/clio_agent/gact/runtime/globals.py": 960,
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
    "src/clio_agent/gact/tool_observer.py": 1074,
    # Collector-collapse work already on this branch grew the file to 1303 (>the
    # recorded 986 baseline) before this entry was updated — pre-existing, not
    # introduced here. P5 (wire semantics): +34 for the waited_tasks union-merge
    # in upsert_repeated_collector_call's tool_call branch (a collapsed re-poll on
    # the same task set must never present fewer resolved rows than either
    # attempt saw). Ratchet back with #714/#767.
    "src/clio_agent/gact/transcript.py": 1337,
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
    "src/clio_agent/gact/turn.py": 838,
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
    "src/clio_agent/gact/turn_finalize.py": 975,  # +36 (A4 #1057): the loop-goal compose glue is extracted into the named, tested `compose_goal_loop_stop_at_finalize` seam (A4 review: the inline glue was silently deletable — the extracted function is driven by the finalize seam test); the glue owns the goal->loop import so goal.py stays a leaf (no cycle)
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
    "src/clio_agent/providers/claude_code_litellm.py": 835,
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
    "src/clio_agent/runtime/status.py": 1240,
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
    "src/clio_agent/tools/execution.py": 1171,
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
