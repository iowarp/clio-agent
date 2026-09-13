# Configuration Knob Audit B — clio-agent src/clio_agent/

Auditor: Claude (Sonnet 4.6), independent sweep.
Scope: `src/clio_agent/` — agent/product code only. Read-only.

---

## OK_REFLECTED

Keys that use `conf.resolve` AND appear (set or documented as unset) in `config.defaults.yaml`.

OK_REFLECTED | a2ui.max_message_bytes | src/clio_agent/gact/a2ui.py:42 | CLIO_A2UI_MAX_MESSAGE_BYTES | conf.resolve; set in defaults (262144)
OK_REFLECTED | a2ui.max_string_chars | src/clio_agent/gact/a2ui.py:60 | CLIO_A2UI_MAX_STRING_CHARS | conf.resolve; set in defaults (16384)
OK_REFLECTED | a2ui.ledger_retention.a2ui_messages.max | src/clio_agent/gact/a2ui.py:79 | CLIO_LEDGER_A2UI_MESSAGES_MAX | conf.resolve; key gact.ledger_retention.a2ui_messages.max set in defaults (512)
OK_REFLECTED | agent_tasks.max_concurrent | src/clio_agent/gact/turn_spawn_executor.py:28 | CLIO_MAX_CONCURRENT_AGENT_TASKS | conf.resolve; set in defaults (3)
OK_REFLECTED | agents.default_blueprint_id | src/clio_agent/gact/agent_blueprint_refresh.py:76 | CLIO_DEFAULT_AGENT_BLUEPRINT_ID | conf.resolve; set in defaults ("base-agent")
OK_REFLECTED | agents.disable_default_registry_bootstrap | src/clio_agent/gact/agent_blueprint_refresh.py:110 | CLIO_AGENT_DISABLE_DEFAULT_REGISTRY_BOOTSTRAP | conf.resolve; set in defaults (false)
OK_REFLECTED | arc.cache_capacity | src/clio_agent/arc/memory.py:195 | CLIO_ARC_CACHE_CAPACITY | conf.resolve; set in defaults (1000)
OK_REFLECTED | arc.clio_core.daemon_recycle_enabled | src/clio_agent/arc/clio_core_daemon.py:193 | CLIO_ARC_CLIO_CORE_DAEMON_RECYCLE | conf.resolve; set in defaults (false)
OK_REFLECTED | arc.clio_core.daemon_rss_critical_bytes | src/clio_agent/arc/clio_core_daemon.py:154 | CLIO_ARC_CLIO_CORE_DAEMON_RSS_CRITICAL | conf.resolve; set in defaults (4294967296)
OK_REFLECTED | arc.clio_core.daemon_rss_warn_bytes | src/clio_agent/arc/clio_core_daemon.py:148 | CLIO_ARC_CLIO_CORE_DAEMON_RSS_WARN | conf.resolve; set in defaults (1073741824)
OK_REFLECTED | arc.clio_core.liveness_ttl_s | src/clio_agent/arc/clio_core_liveness.py:176 | CLIO_ARC_CLIO_CORE_LIVENESS_TTL_S | conf.resolve; set in defaults (3.0)
OK_REFLECTED | arc.clio_core_write_retry.attempts | src/clio_agent/arc/clio_core_retry.py:58 | CLIO_ARC_CLIO_CORE_WRITE_RETRY_ATTEMPTS | conf.resolve; set in defaults (3)
OK_REFLECTED | arc.clio_core_write_retry.backoff_factor | src/clio_agent/arc/clio_core_retry.py:77 | CLIO_ARC_CLIO_CORE_WRITE_RETRY_BACKOFF_FACTOR | conf.resolve; set in defaults (3.0)
OK_REFLECTED | arc.clio_core_write_retry.first_delay_s | src/clio_agent/arc/clio_core_retry.py:96 | CLIO_ARC_CLIO_CORE_WRITE_RETRY_FIRST_DELAY_S | conf.resolve; set in defaults (0.2)
OK_REFLECTED | arc.core_port | src/clio_agent/arc/clio_core_liveness.py:133 | CLIO_CORE_PORT | conf.resolve; documented unset in defaults
OK_REFLECTED | arc.cte.dir | src/clio_agent/arc/clio_core_config.py:200 | CLIO_ARC_CTE_DIR | conf.resolve; documented unset in defaults
OK_REFLECTED | arc.cte.disk_warn_fraction | src/clio_agent/arc/clio_core_config.py:567 | CLIO_ARC_CTE_DISK_WARN_FRACTION | conf.resolve; set in defaults (0.5)
OK_REFLECTED | arc.cte.file_capacity | src/clio_agent/arc/clio_core_config.py:227 | CLIO_ARC_CTE_FILE_CAPACITY | conf.resolve; set in defaults ("50GB")
OK_REFLECTED | arc.cte.ram_capacity | src/clio_agent/arc/clio_core_config.py:249 | CLIO_ARC_CTE_RAM_CAPACITY | conf.resolve; set in defaults ("1GB")
OK_REFLECTED | arc.events_chunk_segments | src/clio_agent/arc/memory.py:163 | CLIO_ARC_EVENTS_CHUNK_SEGMENTS | conf.resolve; set in defaults (512)
OK_REFLECTED | arc.liveness.backoff_initial_s | src/clio_agent/arc/rpc_liveness.py:138 | CLIO_ARC_LIVENESS_BACKOFF_INITIAL_S | conf.resolve; set in defaults (2.0)
OK_REFLECTED | arc.liveness.backoff_max_s | src/clio_agent/arc/rpc_liveness.py:148 | CLIO_ARC_LIVENESS_BACKOFF_MAX_S | conf.resolve; set in defaults (15.0)
OK_REFLECTED | arc.liveness.retries | src/clio_agent/arc/rpc_liveness.py:128 | CLIO_ARC_LIVENESS_RETRIES | conf.resolve; set in defaults (3)
OK_REFLECTED | arc.liveness.stall_after_s | src/clio_agent/arc/rpc_liveness.py:118 | CLIO_ARC_LIVENESS_STALL_AFTER_S | conf.resolve; set in defaults (30.0)
OK_REFLECTED | arc.lsm_compaction_threshold | src/clio_agent/arc/memory.py:222 | CLIO_ARC_LSM_COMPACTION_THRESHOLD | conf.resolve; set in defaults (5)
OK_REFLECTED | arc.lsm_memtable_size | src/clio_agent/arc/memory.py:216 | CLIO_ARC_LSM_MEMTABLE_SIZE | conf.resolve; set in defaults (1000)
OK_REFLECTED | arc.server_conf | src/clio_agent/arc/clio_core_liveness.py:142 | CLIO_SERVER_CONF | conf.resolve; documented unset in defaults
OK_REFLECTED | arc.store | src/clio_agent/arc/storage.py:865 | CLIO_ARC_STORE | conf.resolve; set in defaults ("cte")
OK_REFLECTED | arc.store_config | src/clio_agent/arc/storage.py:875 | CLIO_ARC_STORE_CONFIG | conf.resolve; documented unset in defaults
OK_REFLECTED | artifacts.cas_budget_bytes | src/clio_agent/gact/artifacts/cas.py:79 | CLIO_ARTIFACT_CAS_BUDGET_BYTES | conf.resolve; set in defaults (536870912)
OK_REFLECTED | artifacts.cas_max_file_bytes | src/clio_agent/gact/artifacts/cas.py:66 | CLIO_ARTIFACT_CAS_MAX_FILE_BYTES | conf.resolve; set in defaults (16777216)
OK_REFLECTED | artifacts.export_license | src/clio_agent/gact/artifacts/export.py:78 | CLIO_ARTIFACTS_EXPORT_LICENSE | conf.resolve; set in defaults ("NOASSERTION")
OK_REFLECTED | artifacts.hash_max_file_bytes | src/clio_agent/gact/artifacts/minting.py:152 | CLIO_ARTIFACTS_HASH_MAX_FILE_BYTES | conf.resolve; set in defaults (67108864)
OK_REFLECTED | artifacts.hash_stat_cache | src/clio_agent/gact/artifacts/cas.py:94 | CLIO_ARTIFACT_HASH_STAT_CACHE | conf.resolve; set in defaults (false)
OK_REFLECTED | artifacts.instrument_arg_max_bytes | src/clio_agent/gact/artifacts/transforms.py:100 | CLIO_ARTIFACTS_INSTRUMENT_ARG_MAX_BYTES | conf.resolve; set in defaults (2048)
OK_REFLECTED | artifacts.instrument_total_max_bytes | src/clio_agent/gact/artifacts/transforms.py:90 | CLIO_ARTIFACTS_INSTRUMENT_TOTAL_MAX_BYTES | conf.resolve; set in defaults (16384)
OK_REFLECTED | artifacts.lineage_max_nodes | src/clio_agent/gact/artifacts/lineage.py:49 | CLIO_ARTIFACTS_LINEAGE_MAX_NODES | conf.resolve; set in defaults (500)
OK_REFLECTED | artifacts.proposals_batch_max | src/clio_agent/gact/artifacts/proposals.py:102 | CLIO_ARTIFACTS_PROPOSALS_BATCH_MAX | conf.resolve; set in defaults (32)
OK_REFLECTED | artifacts.proposals_per_turn | src/clio_agent/gact/artifacts/proposals.py:112 | CLIO_ARTIFACTS_PROPOSALS_PER_TURN | conf.resolve; set in defaults (8)
OK_REFLECTED | artifacts.table_preview_max_rows | src/clio_agent/gact/routes/artifact_table_preview.py:30 | CLIO_ARTIFACTS_TABLE_PREVIEW_MAX_ROWS | conf.resolve; set in defaults (2000)
OK_REFLECTED | artifacts.table_preview_max_source_bytes | src/clio_agent/gact/routes/artifact_table_preview.py:49 | CLIO_ARTIFACTS_TABLE_PREVIEW_MAX_SOURCE_BYTES | conf.resolve; set in defaults (268435456)
OK_REFLECTED | autocompact.pct | src/clio_agent/gact/runtime/context_tokens.py:67 | CLIO_AUTOCOMPACT_PCT | conf.resolve; set in defaults ("0.85")
OK_REFLECTED | debug.dump_unparseable | src/clio_agent/lm/adapters.py:146 | CLIO_DUMP_UNPARSEABLE | conf.resolve; documented unset in defaults
OK_REFLECTED | debug.level | src/clio_agent/runtime/trace.py:113 | CLIO_DEBUG | conf.resolve; set in defaults ("low")
OK_REFLECTED | debug.lm_response | src/clio_agent/runtime/trace.py:129 | CLIO_LOG_LM_RESPONSE | conf.resolve; set in defaults (false)
OK_REFLECTED | debug.memprof | src/clio_agent/gact/diagnostics.py:137 | CLIO_DEBUG_MEMPROF | conf.resolve; set in defaults (false)
OK_REFLECTED | debug.memprof_frames | src/clio_agent/gact/diagnostics.py:49 | CLIO_DEBUG_MEMPROF_FRAMES | conf.resolve; set in defaults (20)
OK_REFLECTED | debug.memprof_out | src/clio_agent/gact/diagnostics.py:41 | CLIO_DEBUG_MEMPROF_OUT | conf.resolve; documented unset in defaults
OK_REFLECTED | debug.sse_event_log | src/clio_agent/gact/routes/misc.py:83 | CLIO_SSE_EVENT_LOG | conf.resolve; documented unset in defaults
OK_REFLECTED | debug.sse_wire_tap | src/clio_agent/gact/routes/misc.py:63 | CLIO_SSE_WIRE_TAP | conf.resolve; documented unset in defaults
OK_REFLECTED | debug.stream_audit_log | src/clio_agent/runtime/stream_audit.py:40 | CLIO_STREAM_AUDIT_LOG | conf.resolve; documented unset in defaults
OK_REFLECTED | gact.ask_user.max_ttl_s | src/clio_agent/gact/ask_user_tool.py:62 | CLIO_ASK_USER_MAX_TTL_S | conf.resolve; set in defaults (86400)
OK_REFLECTED | gact.ask_user.ttl_s | src/clio_agent/gact/ask_user_tool.py:56 | CLIO_ASK_USER_TTL_S | conf.resolve; set in defaults (600)
OK_REFLECTED | gact.auth.bearer_token | src/clio_agent/gact/auth.py:24 | CLIO_GACT_BEARER_TOKEN | conf.resolve; documented unset in defaults
OK_REFLECTED | gact.blueprint_registry.url | src/clio_agent/gact/agent_blueprints.py:109 | CLIO_BLUEPRINT_REGISTRY_URL | conf.resolve; set in defaults
OK_REFLECTED | gact.blueprint_source.clone_timeout_s | src/clio_agent/gact/agent_blueprint_sources.py:74 | CLIO_BLUEPRINT_SOURCE_CLONE_TIMEOUT_S | conf.resolve; set in defaults (30.0)
OK_REFLECTED | gact.cancellation_grace_s | src/clio_agent/gact/routes/session_cancellation.py:32 | CLIO_GACT_CANCELLATION_GRACE_S | conf.resolve; set in defaults (0.1)
OK_REFLECTED | gact.context_references.browse_limit_per_kind | src/clio_agent/gact/context_reference_search.py:35 | CLIO_CONTEXT_REFERENCE_BROWSE_LIMIT | conf.resolve; set in defaults (20)
OK_REFLECTED | gact.context_references.max_hashable_bytes | src/clio_agent/gact/context_references.py:88 | CLIO_CONTEXT_REFERENCE_MAX_HASHABLE_BYTES | conf.resolve; set in defaults (67108864)
OK_REFLECTED | gact.context_references.search_limit | src/clio_agent/gact/context_reference_search.py:50 | CLIO_CONTEXT_REFERENCE_SEARCH_LIMIT | conf.resolve; set in defaults (100)
OK_REFLECTED | gact.context_references.snapshot_children | src/clio_agent/gact/context_reference_evidence.py:50 | CLIO_CONTEXT_REFERENCE_SNAPSHOT_CHILDREN | conf.resolve; set in defaults (50)
OK_REFLECTED | gact.context_references.snapshot_string_chars | src/clio_agent/gact/context_reference_evidence.py:65 | CLIO_CONTEXT_REFERENCE_SNAPSHOT_STRING_CHARS | conf.resolve; set in defaults (4000)
OK_REFLECTED | gact.context_references.summary_excerpt_chars | src/clio_agent/gact/context_references.py:53 | CLIO_CONTEXT_REFERENCE_SUMMARY_EXCERPT_CHARS | conf.resolve; set in defaults (600)
OK_REFLECTED | gact.context_references.summary_messages | src/clio_agent/gact/context_references.py:68 | CLIO_CONTEXT_REFERENCE_SUMMARY_MESSAGES | conf.resolve; set in defaults (5)
OK_REFLECTED | gact.cors.origins | src/clio_agent/gact/cors.py:26 | CLIO_GACT_CORS_ORIGINS | conf.resolve; documented unset in defaults
OK_REFLECTED | gact.interactions.projection_limit | src/clio_agent/gact/routes/interactions.py:472 | CLIO_INTERACTIONS_PROJECTION_LIMIT | conf.resolve; set in defaults (200)
OK_REFLECTED | gact.ledger_retention.command_audit.max | src/clio_agent/gact/runtime/retention.py (via key) | CLIO_LEDGER_COMMAND_AUDIT_MAX | conf.resolve; set in defaults (2000)
OK_REFLECTED | gact.ledger_retention.context_frames.max | src/clio_agent/gact/runtime/retention.py | CLIO_LEDGER_CONTEXT_FRAMES_MAX | conf.resolve; set in defaults (200)
OK_REFLECTED | gact.ledger_retention.memory_tool_audit.max | src/clio_agent/gact/runtime/retention.py | CLIO_LEDGER_MEMORY_TOOL_AUDIT_MAX | conf.resolve; set in defaults (2000)
OK_REFLECTED | gact.ledger_retention.native_delivery_notes.max | src/clio_agent/gact/native_delivery_outcome.py:83 | CLIO_LEDGER_NATIVE_DELIVERY_NOTES_MAX | conf.resolve; set in defaults (256)
OK_REFLECTED | gact.ledger_retention.pending_diffs.hard | src/clio_agent/gact/runtime/retention.py | CLIO_LEDGER_PENDING_DIFFS_HARD | conf.resolve; set in defaults (1000)
OK_REFLECTED | gact.ledger_retention.pending_diffs.max | src/clio_agent/gact/runtime/retention.py | CLIO_LEDGER_PENDING_DIFFS_MAX | conf.resolve; set in defaults (500)
OK_REFLECTED | gact.ledger_retention.permissions.hard | src/clio_agent/gact/runtime/retention.py | CLIO_LEDGER_PERMISSIONS_HARD | conf.resolve; set in defaults (4000)
OK_REFLECTED | gact.ledger_retention.permissions.max | src/clio_agent/gact/runtime/retention.py | CLIO_LEDGER_PERMISSIONS_MAX | conf.resolve; set in defaults (2000)
OK_REFLECTED | gact.ledger_retention.shared_tokens.hard | src/clio_agent/gact/runtime/retention.py | CLIO_LEDGER_SHARED_TOKENS_HARD | conf.resolve; set in defaults (10000)
OK_REFLECTED | gact.ledger_retention.shared_tokens.max | src/clio_agent/gact/runtime/retention.py | CLIO_LEDGER_SHARED_TOKENS_MAX | conf.resolve; set in defaults (5000)
OK_REFLECTED | gact.ledger_retention.stream_fallback_notes.max | src/clio_agent/gact/stream_fallbacks.py:87 | CLIO_LEDGER_STREAM_FALLBACK_NOTES_MAX | conf.resolve; set in defaults (32)
OK_REFLECTED | gact.ledger_retention.turn_attempts.hard | src/clio_agent/gact/runtime/retention.py | CLIO_LEDGER_TURN_ATTEMPTS_HARD | conf.resolve; set in defaults (4000)
OK_REFLECTED | gact.ledger_retention.turn_attempts.max | src/clio_agent/gact/runtime/retention.py | CLIO_LEDGER_TURN_ATTEMPTS_MAX | conf.resolve; set in defaults (2000)
OK_REFLECTED | gact.ledger_retention.user_questions.hard | src/clio_agent/gact/runtime/retention.py | CLIO_LEDGER_USER_QUESTIONS_HARD | conf.resolve; set in defaults (4000)
OK_REFLECTED | gact.ledger_retention.user_questions.max | src/clio_agent/gact/runtime/retention.py | CLIO_LEDGER_USER_QUESTIONS_MAX | conf.resolve; set in defaults (2000)
OK_REFLECTED | gact.live_edge_streaming | src/clio_agent/gact/live_edge.py:400 | CLIO_LIVE_EDGE_STREAMING | conf.resolve; set in defaults (false)
OK_REFLECTED | gact.loop_inbox.max_events | src/clio_agent/gact/loop_inbox.py:97 | CLIO_GACT_LOOP_INBOX_MAX_EVENTS | conf.resolve; set in defaults (64)
OK_REFLECTED | gact.message_intents.max_acceptances_per_session | src/clio_agent/gact/message_intents.py:67 | CLIO_GACT_MAX_ACCEPTANCES_PER_SESSION | conf.resolve; set in defaults (200)
OK_REFLECTED | gact.message_intents.max_queued_per_session | src/clio_agent/gact/message_intents.py:55 | CLIO_GACT_MAX_QUEUED_MESSAGES_PER_SESSION | conf.resolve; set in defaults (100)
OK_REFLECTED | gact.message_intents.max_settled_steers_per_session | src/clio_agent/gact/message_intents.py:61 | CLIO_GACT_MAX_SETTLED_STEERS_PER_SESSION | conf.resolve; set in defaults (100)
OK_REFLECTED | gact.resident_ledgers.idle_ttl_s | src/clio_agent/gact/resident_ledgers.py:222 | CLIO_RESIDENT_LEDGERS_TTL_S | conf.resolve; set in defaults (1800.0)
OK_REFLECTED | gact.resident_ledgers.max_bytes | src/clio_agent/gact/resident_ledgers.py:240 | CLIO_RESIDENT_LEDGERS_MAX_BYTES | conf.resolve; set in defaults (536870912)
OK_REFLECTED | gact.resident_ledgers.max_sessions | src/clio_agent/gact/resident_ledgers.py (via retention) | CLIO_RESIDENT_LEDGERS_MAX | conf.resolve; set in defaults (512)
OK_REFLECTED | goal.judge_model | src/clio_agent/gact/goal.py:172 | CLIO_GOAL_JUDGE_MODEL | conf.resolve; documented unset in defaults
OK_REFLECTED | hooks.allow_managed_only | src/clio_agent/gact/hooks/dispatcher.py:317 | CLIO_HOOKS_ALLOW_MANAGED_ONLY | conf.resolve; set in defaults (false)
OK_REFLECTED | hooks.config | src/clio_agent/gact/hooks/dispatcher.py:311 | CLIO_HOOKS_CONFIG | conf.resolve; documented unset in defaults
OK_REFLECTED | hooks.defer_timeout | src/clio_agent/gact/hooks/defer.py:121 | CLIO_HOOKS_DEFER_TIMEOUT | conf.resolve; set in defaults (86400.0)
OK_REFLECTED | hooks.managed_config | src/clio_agent/gact/hooks/dispatcher.py:314 | CLIO_HOOKS_MANAGED_CONFIG | conf.resolve; documented unset in defaults
OK_REFLECTED | hooks.stop_loop_cap | src/clio_agent/gact/hooks/stop_loop.py:71 | CLIO_HOOKS_STOP_LOOP_CAP | conf.resolve; set in defaults (8)
OK_REFLECTED | hooks.trust_store | src/clio_agent/gact/hooks/dispatcher.py:340 | CLIO_HOOKS_TRUST_STORE | conf.resolve; documented unset in defaults
OK_REFLECTED | limits.agent_task_artifact_context_chars | src/clio_agent/gact/agent_task_artifacts.py:251 | CLIO_AGENT_TASK_ARTIFACT_CONTEXT_CHARS | conf.resolve; set in defaults (64000)
OK_REFLECTED | limits.agent_task_output_digest_chars | src/clio_agent/gact/agents/agent_task_output_digest.py:78 | CLIO_AGENT_TASK_OUTPUT_DIGEST_CHARS | conf.resolve; set in defaults (8000)
OK_REFLECTED | limits.codex_sdk_progress_timeout_s | src/clio_agent/providers/codex_stream.py:145 | CLIO_CODEX_SDK_PROGRESS_TIMEOUT_S | conf.resolve; set in defaults (120.0)
OK_REFLECTED | limits.context_inline_bytes | src/clio_agent/gact/runtime/constants.py:82 | CLIO_CTX_MAX_BYTES | conf.resolve; set in defaults (32768)
OK_REFLECTED | limits.fs_read_bytes | src/clio_agent/tools/servers/fs_server.py:41 | CLIO_FS_MAX_READ_BYTES | conf.resolve; set in defaults (262144)
OK_REFLECTED | limits.lm_call_s | src/clio_agent/runtime/lm_activity.py:411 | CLIO_MAX_LM_CALL_S | conf.resolve; set in defaults (1800.0)
OK_REFLECTED | limits.lm_inter_token_idle_s | src/clio_agent/runtime/lm_activity.py:424 | CLIO_LM_INTER_TOKEN_IDLE_S | conf.resolve; set in defaults (120.0)
OK_REFLECTED | limits.lm_parse_retry_attempts | src/clio_agent/lm/adapters.py:417 | CLIO_LM_PARSE_RETRY_ATTEMPTS | conf.resolve; documented unset in defaults
OK_REFLECTED | limits.lm_transient_backoff_s | src/clio_agent/lm/io_logging.py:155 | CLIO_LM_TRANSIENT_BACKOFF_S | conf.resolve; set in defaults (8.0)
OK_REFLECTED | limits.lm_transient_retries | src/clio_agent/lm/io_logging.py:137 | CLIO_LM_TRANSIENT_RETRIES | conf.resolve; set in defaults (2.0)
OK_REFLECTED | limits.mcp_content_block_max_bytes | src/clio_agent/tools/mcp_results.py:62 | CLIO_MCP_CONTENT_BLOCK_MAX_BYTES | conf.resolve; set in defaults (524288)
OK_REFLECTED | limits.mcp_reconnect_timeout_s | src/clio_agent/gact/routes/mcp.py:92 | CLIO_GACT_MCP_RECONNECT_TIMEOUT_S | conf.resolve; set in defaults (15.0)
OK_REFLECTED | limits.model_tool_result_chars | src/clio_agent/tools/mcp_result_projection.py:35 | CLIO_MODEL_TOOL_RESULT_CHARS | conf.resolve; set in defaults (12000)
OK_REFLECTED | limits.plan_review_chars | src/clio_agent/gact/plan_review.py:25 | CLIO_PLAN_REVIEW_CHARS | conf.resolve; set in defaults (256000)
OK_REFLECTED | limits.shell_default_output_bytes | src/clio_agent/tools/servers/shell_server.py:67 | CLIO_SHELL_DEFAULT_OUTPUT_BYTES | conf.resolve; set in defaults (16384)
OK_REFLECTED | limits.shell_default_timeout_s | src/clio_agent/tools/servers/shell_server.py:58 | CLIO_SHELL_DEFAULT_TIMEOUT_S | conf.resolve; set in defaults (5.0)
OK_REFLECTED | limits.shell_max_command_chars | src/clio_agent/tools/servers/shell_server.py:79 | CLIO_SHELL_MAX_COMMAND_CHARS | conf.resolve; set in defaults (4000)
OK_REFLECTED | limits.shell_max_output_bytes | src/clio_agent/tools/servers/shell_server.py:73 | CLIO_SHELL_MAX_OUTPUT_BYTES | conf.resolve; set in defaults (131072)
OK_REFLECTED | limits.shell_max_timeout_s | src/clio_agent/tools/servers/shell_server.py:64 | CLIO_SHELL_MAX_TIMEOUT_S | conf.resolve; set in defaults (30.0)
OK_REFLECTED | limits.tool_result_chars | src/clio_agent/gact/evidence.py:126 | CLIO_TOOL_RESULT_CHARS | conf.resolve; set in defaults (12000)
OK_REFLECTED | limits.transient_provider_retry_delays | src/clio_agent/agent.py:865 | CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS | conf.resolve; documented unset in defaults
OK_REFLECTED | limits.turn_timeout_s | src/clio_agent/gact/_params.py:103 | CLIO_GACT_TURN_TIMEOUT_S | conf.resolve; set in defaults (900.0)
OK_REFLECTED | lm.api_base | src/clio_agent/config.py:532 | CLIO_LM_API_BASE | conf.resolve; documented unset in defaults
OK_REFLECTED | lm.claude_code_transport | src/clio_agent/config.py:545 | CLIO_CLAUDE_CODE_TRANSPORT | conf.resolve; documented unset in defaults
OK_REFLECTED | lm.codex_transport | src/clio_agent/config.py:540 | CLIO_CODEX_TRANSPORT | conf.resolve; documented unset in defaults
OK_REFLECTED | lm.context_window | src/clio_agent/config.py:407 | CLIO_LM_CONTEXT_WINDOW | conf.resolve; set in defaults (0)
OK_REFLECTED | lm.defer_tiktoken | src/clio_agent/lm/factory.py:36 | CLIO_LM_DEFER_TIKTOKEN | conf.resolve; set in defaults (true)
OK_REFLECTED | lm.disable_json_adapter_fallback | src/clio_agent/lm/adapters.py:813 | CLIO_DISABLE_JSON_ADAPTER_FALLBACK | conf.resolve; set in defaults (false)
OK_REFLECTED | lm.disable_thinking | src/clio_agent/lm/factory.py:300 | CLIO_LM_DISABLE_THINKING | conf.resolve; set in defaults (false)
OK_REFLECTED | lm.guided_output | src/clio_agent/lm/adapters.py:417 | CLIO_LM_GUIDED_OUTPUT | conf.resolve; set in defaults (false)
OK_REFLECTED | lm.lmstudio_flash_attention | src/clio_agent/gact/routes/providers.py:92 | CLIO_LMSTUDIO_FLASH_ATTENTION | conf.resolve; set in defaults (true)
OK_REFLECTED | lm.max_tokens | src/clio_agent/config.py:582 | CLIO_LM_MAX_TOKENS | conf.resolve; documented unset in defaults
OK_REFLECTED | lm.min_p | src/clio_agent/config.py:587 | CLIO_LM_MIN_P | conf.resolve; documented unset in defaults
OK_REFLECTED | lm.model | src/clio_agent/config.py:533 | CLIO_LM_MODEL | conf.resolve; documented unset in defaults
OK_REFLECTED | lm.planner_max_tokens | src/clio_agent/config.py:579 | CLIO_LM_PLANNER_MAX_TOKENS | conf.resolve; documented unset in defaults
OK_REFLECTED | lm.planner_temperature | src/clio_agent/config.py:573 | CLIO_LM_PLANNER_TEMPERATURE | conf.resolve; documented unset in defaults
OK_REFLECTED | lm.presence_penalty | src/clio_agent/config.py:588 | CLIO_LM_PRESENCE_PENALTY | conf.resolve; documented unset in defaults
OK_REFLECTED | lm.provider | src/clio_agent/config.py:529 | CLIO_LM_PROVIDER | conf.resolve; set in defaults ("lm_studio")
OK_REFLECTED | lm.reasoning_model | src/clio_agent/lm/adapters.py:490 | CLIO_LM_REASONING_MODEL | conf.resolve; documented unset in defaults
OK_REFLECTED | lm.stop_sequences | src/clio_agent/lm/factory.py:355 | CLIO_LM_STOP_SEQUENCES | conf.resolve; documented unset in defaults
OK_REFLECTED | lm.temperature | src/clio_agent/config.py:570 | CLIO_LM_TEMPERATURE | conf.resolve; documented unset in defaults
OK_REFLECTED | lm.thinking_budget | src/clio_agent/config.py:564 | CLIO_LM_THINKING_BUDGET | conf.resolve; documented unset in defaults
OK_REFLECTED | lm.thinking_level | src/clio_agent/config.py:558 | CLIO_LM_THINKING_LEVEL | conf.resolve; documented unset in defaults
OK_REFLECTED | lm.top_k | src/clio_agent/config.py:586 | CLIO_LM_TOP_K | conf.resolve; documented unset in defaults
OK_REFLECTED | lm.top_p | src/clio_agent/config.py:585 | CLIO_LM_TOP_P | conf.resolve; documented unset in defaults
OK_REFLECTED | paths.data_dir | src/clio_agent/runtime/status.py:713 | CLIO_DATA_DIR | conf.resolve; set in defaults (".clio/agent")
OK_REFLECTED | paths.model_catalog | src/clio_agent/providers/model_discovery/overlay.py:196 | CLIO_MODEL_CATALOG | conf.resolve; documented unset in defaults
OK_REFLECTED | paths.model_db | src/clio_agent/providers/handshake/sources/db.py:53 | CLIO_MODEL_DB | conf.resolve; documented unset in defaults
OK_REFLECTED | paths.sessions | src/clio_agent/gact/sessions.py:88 | CLIO_SESSIONS_PATH | conf.resolve; documented unset in defaults
OK_REFLECTED | paths.web_dir | src/clio_agent/gact/app.py:145 | CLIO_WEB_DIR | conf.resolve; documented unset in defaults
OK_REFLECTED | permissions.ai_review_model | src/clio_agent/gact/runtime/ai_review.py:126 | CLIO_AI_REVIEW_MODEL | conf.resolve; documented unset in defaults
OK_REFLECTED | permissions.ai_review_timeout_s | src/clio_agent/gact/runtime/ai_review.py:234 | CLIO_AI_REVIEW_TIMEOUT_S | conf.resolve; set in defaults ("45.0")
OK_REFLECTED | provenance.agentic.flowcept.campaign_id | src/clio_agent/gact/provenance/factory.py:152 | CLIO_FLOWCEPT_CAMPAIGN_ID | conf.resolve; documented unset in defaults
OK_REFLECTED | provenance.agentic.flowcept.campaign_scope | src/clio_agent/gact/provenance/factory.py:144 | CLIO_FLOWCEPT_CAMPAIGN_SCOPE | conf.resolve; set in defaults ("session")
OK_REFLECTED | provenance.agentic.flowcept.check_safe_stops | src/clio_agent/gact/provenance/factory.py:182 | CLIO_FLOWCEPT_CHECK_SAFE_STOPS | conf.resolve; set in defaults (true)
OK_REFLECTED | provenance.agentic.flowcept.exclude_events | src/clio_agent/gact/provenance/factory.py:167 | CLIO_FLOWCEPT_EXCLUDE_EVENTS | conf.resolve; set in defaults (["lm.token.delta","thinking.*"])
OK_REFLECTED | provenance.agentic.flowcept.include_events | src/clio_agent/gact/provenance/factory.py:175 | CLIO_FLOWCEPT_INCLUDE_EVENTS | conf.resolve; set in defaults (["*"])
OK_REFLECTED | provenance.agentic.flowcept.privacy | src/clio_agent/gact/provenance/factory.py:158 | CLIO_FLOWCEPT_PRIVACY | conf.resolve; set in defaults ("metadata")
OK_REFLECTED | provenance.agentic.flowcept.workflow_scope | src/clio_agent/gact/provenance/factory.py:136 | CLIO_FLOWCEPT_WORKFLOW_SCOPE | conf.resolve; set in defaults ("session")
OK_REFLECTED | provenance.agentic.jsonl.path | src/clio_agent/gact/provenance/factory.py:101 | CLIO_PROVENANCE_JSONL_PATH | conf.resolve; documented unset in defaults
OK_REFLECTED | provenance.agentic.providers | src/clio_agent/provenance_config.py:52 | CLIO_PROVENANCE_PROVIDERS | conf.resolve; set in defaults (["jsonl"])
OK_REFLECTED | provenance.agentic.query_default | src/clio_agent/gact/routes/provenance.py:134 | CLIO_PROVENANCE_QUERY_DEFAULT | conf.resolve; set in defaults ("native")
OK_REFLECTED | provenance.agentic.queue_size | src/clio_agent/gact/provenance/factory.py:117 | CLIO_PROVENANCE_QUEUE_SIZE | conf.resolve; set in defaults (4096)
OK_REFLECTED | provenance.artifacts.cmf.publish_timeout_s | src/clio_agent/gact/artifacts/provenance/cmf_mode.py:160 | CLIO_CMF_PUBLISH_TIMEOUT_S | conf.resolve; set in defaults (30.0)
OK_REFLECTED | provenance.artifacts.native.storage | src/clio_agent/gact/artifacts/provenance/factory.py:70 | CLIO_NATIVE_ARTIFACT_STORE | conf.resolve; set in defaults ("file")
OK_REFLECTED | provenance.artifacts.provider | src/clio_agent/gact/artifacts/provenance/factory.py:22 | CLIO_ARTIFACT_PROVENANCE_PROVIDER | conf.resolve; set in defaults ("native")
OK_REFLECTED | provenance.artifacts.queue_size | src/clio_agent/gact/artifacts/provenance/factory.py:77 | CLIO_ARTIFACT_PROVENANCE_QUEUE_SIZE | conf.resolve; set in defaults (4096)
OK_REFLECTED | provenance.kvnorm | src/clio_agent/provenance_config.py:84 | CLIO_PROVENANCE_KVNORM | conf.resolve; set in defaults (false)
OK_REFLECTED | providers.claude_code.max_concurrent_processes | src/clio_agent/providers/claude_code_stream_bounds.py:95 | CLIO_CLAUDE_CODE_MAX_CONCURRENT_PROCESSES | conf.resolve; set in defaults (4.0)
OK_REFLECTED | providers.claude_code.probe_timeout_s | src/clio_agent/providers/model_discovery/claude_code.py:49 | CLIO_CLAUDE_CODE_PROBE_TIMEOUT_S | conf.resolve; set in defaults (30.0)
OK_REFLECTED | providers.claude_code.session_reuse | src/clio_agent/providers/claude_code_sessions.py:264 | CLIO_CLAUDE_CODE_SESSION_REUSE | conf.resolve; set in defaults (true)
OK_REFLECTED | providers.claude_code.stateful_capacity | src/clio_agent/providers/claude_code_stateful.py:107 | CLIO_CLAUDE_CODE_STATEFUL_CAPACITY | conf.resolve; set in defaults (128.0)
OK_REFLECTED | providers.claude_code.stream_idle_ttl_s | src/clio_agent/providers/claude_code_stream_bounds.py:158 | CLIO_CLAUDE_CODE_STREAM_IDLE_TTL_S | conf.resolve; set in defaults (15.0)
OK_REFLECTED | providers.codex.credential_home_capacity | src/clio_agent/providers/codex_credential_home.py:42 | CLIO_CODEX_CREDENTIAL_HOME_CAPACITY | conf.resolve; set in defaults (4)
OK_REFLECTED | providers.model_catalog_ttl_s | src/clio_agent/providers/model_discovery/overlay.py:84 | CLIO_MODEL_CATALOG_TTL_S | conf.resolve; set in defaults (86400.0)
OK_REFLECTED | providers.native_image_url_allowlist | src/clio_agent/providers/claude_code_multimodal.py:64 | CLIO_PROVIDER_NATIVE_IMAGE_URL_ALLOWLIST | conf.resolve; documented unset in defaults
OK_REFLECTED | relay.cluster | src/clio_agent/tools/relay_factory.py:294 | CLIO_RELAY_CLUSTER | conf.resolve; documented unset in defaults
OK_REFLECTED | relay.console.enabled | src/clio_agent/tools/relay_console.py:111 | CLIO_RELAY_CONSOLE_ENABLED | conf.resolve; set in defaults (true)
OK_REFLECTED | relay.console.pull_limit_bytes | src/clio_agent/tools/relay_console.py:128 | CLIO_RELAY_CONSOLE_PULL_LIMIT_BYTES | conf.resolve; set in defaults (65536)
OK_REFLECTED | relay.console.stream | src/clio_agent/tools/relay_console.py:148 | CLIO_RELAY_CONSOLE_STREAM | conf.resolve; set in defaults ("console")
OK_REFLECTED | relay.console.tail_cap_bytes | src/clio_agent/tools/relay_console.py:167 | CLIO_RELAY_CONSOLE_TAIL_CAP_BYTES | conf.resolve; set in defaults (8192)
OK_REFLECTED | relay.fetch_max_bytes | src/clio_agent/tools/relay_artifact_fetch.py:68 | CLIO_RELAY_FETCH_MAX_BYTES | conf.resolve; set in defaults (104857600)
OK_REFLECTED | relay.http_url | src/clio_agent/tools/relay_factory.py:232 | CLIO_RELAY_HTTP_URL | conf.resolve; documented unset in defaults
OK_REFLECTED | relay.install_surface.attention_idle_seconds | src/clio_agent/tools/relay_cli_runner.py:303 | CLIO_RELAY_INSTALL_ATTENTION_IDLE_S | conf.resolve; set in defaults (45.0)
OK_REFLECTED | relay.install_surface.bounded_timeout_seconds | src/clio_agent/tools/relay_cli_runner.py:316 | CLIO_RELAY_INSTALL_BOUNDED_TIMEOUT_S | conf.resolve; set in defaults (60.0)
OK_REFLECTED | relay.install_surface.cli_path | src/clio_agent/tools/relay_cli_runner.py:271 | CLIO_RELAY_CLI_PATH | conf.resolve; documented unset in defaults
OK_REFLECTED | relay.install_surface.job_retention_hard_cap | src/clio_agent/tools/relay_cli_runner.py:388 | CLIO_RELAY_INSTALL_JOB_RETENTION_HARD_CAP | conf.resolve; set in defaults (400)
OK_REFLECTED | relay.install_surface.job_retention_max | src/clio_agent/tools/relay_cli_runner.py:373 | CLIO_RELAY_INSTALL_JOB_RETENTION_MAX | conf.resolve; set in defaults (200)
OK_REFLECTED | relay.install_surface.long_operation_timeout_seconds | src/clio_agent/tools/relay_cli_runner.py:345 | CLIO_RELAY_INSTALL_LONG_OP_TIMEOUT_S | conf.resolve; set in defaults (900.0)
OK_REFLECTED | relay.install_surface.output_tail_bytes | src/clio_agent/tools/relay_cli_runner.py:332 | CLIO_RELAY_INSTALL_OUTPUT_TAIL_BYTES | conf.resolve; set in defaults (4096)
OK_REFLECTED | relay.install_surface.parsed_document_max_bytes | src/clio_agent/tools/relay_cli_runner.py:403 | CLIO_RELAY_INSTALL_PARSED_DOCUMENT_MAX_BYTES | conf.resolve; set in defaults (262144)
OK_REFLECTED | relay.jarvis_door_namespace | src/clio_agent/tools/relay_factory.py:319 | CLIO_RELAY_JARVIS_DOOR_NAMESPACE | conf.resolve; set in defaults ("remote_jarvis")
OK_REFLECTED | relay.mcp_url | src/clio_agent/tools/relay_factory.py:229 | CLIO_RELAY_MCP_URL | conf.resolve; documented unset in defaults
OK_REFLECTED | relay.owner_session_generation_id | src/clio_agent/tools/relay_factory.py:253 | CLIO_RELAY_SESSION_GENERATION_ID | conf.resolve; documented unset in defaults
OK_REFLECTED | relay.owner_session_id | src/clio_agent/tools/relay_factory.py:250 | CLIO_RELAY_OWNER_SESSION_ID | conf.resolve; documented unset in defaults
OK_REFLECTED | relay.remote_agent.mcp_config_path | src/clio_agent/gact/relay_wiring.py:338 | CLIO_RELAY_REMOTE_AGENT_MCP_CONFIG_PATH | conf.resolve; documented unset in defaults
OK_REFLECTED | relay.remote_agent.model | src/clio_agent/gact/relay_wiring.py:345 | CLIO_RELAY_REMOTE_AGENT_MODEL | conf.resolve; documented unset in defaults
OK_REFLECTED | relay.remote_agent.prompt_path | src/clio_agent/gact/relay_wiring.py:305 | CLIO_RELAY_REMOTE_AGENT_PROMPT_PATH | conf.resolve; documented unset in defaults
OK_REFLECTED | relay.remote_agent.workdir | src/clio_agent/gact/relay_wiring.py:352 | CLIO_RELAY_REMOTE_AGENT_WORKDIR | conf.resolve; documented unset in defaults
OK_REFLECTED | relay.tool_surfaces_ttl_seconds | src/clio_agent/gact/relay_wiring.py:46 | CLIO_RELAY_TOOL_SURFACES_TTL_SECONDS | conf.resolve; set in defaults (300)
OK_REFLECTED | resources.delivery_ledger_max_records | src/clio_agent/gact/resource_delivery.py:32 | CLIO_RESOURCE_DELIVERY_LEDGER_MAX_RECORDS | conf.resolve; set in defaults (2000)
OK_REFLECTED | resources.derivative_name_max_chars | src/clio_agent/gact/resource_processing.py:688 | CLIO_RESOURCE_DERIVATIVE_NAME_MAX_CHARS | conf.resolve; set in defaults (48)
OK_REFLECTED | resources.document_processor_url | src/clio_agent/gact/composer_runtime.py:52 | CLIO_DOCUMENT_PROCESSOR_URL | conf.resolve; documented unset in defaults
OK_REFLECTED | resources.list_max_records | src/clio_agent/gact/resource_tools.py:58 | CLIO_RESOURCE_LIST_MAX_RECORDS | conf.resolve; set in defaults (100)
OK_REFLECTED | resources.max_bytes | src/clio_agent/gact/composer_runtime.py:39 | CLIO_RESOURCE_MAX_BYTES | conf.resolve; set in defaults (262144000)
OK_REFLECTED | resources.native_attachment_total_max_bytes | src/clio_agent/providers/native_attachment_bounds.py:91 | CLIO_RESOURCE_NATIVE_ATTACHMENT_TOTAL_MAX_BYTES | conf.resolve; set in defaults (33554432)
OK_REFLECTED | resources.native_document_max_bytes | src/clio_agent/providers/native_attachment_bounds.py:75 | CLIO_RESOURCE_NATIVE_DOCUMENT_MAX_BYTES | conf.resolve; set in defaults (33554432)
OK_REFLECTED | resources.native_image_max_bytes | src/clio_agent/providers/native_attachment_bounds.py:59 | CLIO_RESOURCE_NATIVE_IMAGE_MAX_BYTES | conf.resolve; set in defaults (5242880)
OK_REFLECTED | resources.processing_event_max_records | src/clio_agent/gact/resource_processing_bounds.py:46 | CLIO_RESOURCE_PROCESSING_EVENT_MAX_RECORDS | conf.resolve; set in defaults (100)
OK_REFLECTED | resources.processing_event_message_chars | src/clio_agent/gact/resource_processing_bounds.py:55 | CLIO_RESOURCE_PROCESSING_EVENT_MESSAGE_CHARS | conf.resolve; set in defaults (1000)
OK_REFLECTED | resources.processing_event_stage_chars | src/clio_agent/gact/resource_processing_bounds.py:64 | CLIO_RESOURCE_PROCESSING_EVENT_STAGE_CHARS | conf.resolve; set in defaults (80)
OK_REFLECTED | resources.processing_poll_interval_s | src/clio_agent/gact/resource_processing_bounds.py:86 | CLIO_RESOURCE_PROCESSING_POLL_INTERVAL_S | conf.resolve; set in defaults (0.5)
OK_REFLECTED | resources.processor_cancel_timeout_s | src/clio_agent/gact/resource_processing.py:671 | CLIO_RESOURCE_PROCESSOR_CANCEL_TIMEOUT_S | conf.resolve; set in defaults (30.0)
OK_REFLECTED | resources.processor_connect_timeout_s | src/clio_agent/gact/resource_processing.py:645 | CLIO_RESOURCE_PROCESSOR_CONNECT_TIMEOUT_S | conf.resolve; set in defaults (5.0)
OK_REFLECTED | resources.processor_pool_timeout_s | src/clio_agent/gact/resource_processing.py:658 | CLIO_RESOURCE_PROCESSOR_POOL_TIMEOUT_S | conf.resolve; set in defaults (5.0)
OK_REFLECTED | resources.processor_read_timeout_s | src/clio_agent/gact/resource_processing.py:651 | CLIO_RESOURCE_PROCESSOR_READ_TIMEOUT_S | conf.resolve; set in defaults (60.0)
OK_REFLECTED | resources.processor_status_timeout_s | src/clio_agent/gact/resource_processing.py:665 | CLIO_RESOURCE_PROCESSOR_STATUS_TIMEOUT_S | conf.resolve; set in defaults (30.0)
OK_REFLECTED | resources.processor_write_timeout_s | src/clio_agent/gact/resource_processing.py:65 | CLIO_RESOURCE_PROCESSOR_WRITE_TIMEOUT_S | conf.resolve; set in defaults (0.0)
OK_REFLECTED | resources.search_excerpt_chars | src/clio_agent/gact/resource_tools.py:80 | CLIO_RESOURCE_SEARCH_EXCERPT_CHARS | conf.resolve; set in defaults (500)
OK_REFLECTED | resources.search_match_limit | src/clio_agent/gact/resource_tools.py:91 | CLIO_RESOURCE_SEARCH_MATCH_LIMIT | conf.resolve; set in defaults (50)
OK_REFLECTED | resources.status_poll_failure_threshold | src/clio_agent/gact/resource_lifecycle.py:58 | CLIO_RESOURCE_STATUS_POLL_FAILURE_THRESHOLD | conf.resolve; set in defaults (5)
OK_REFLECTED | resources.structure_node_max_bytes | src/clio_agent/gact/resource_processing.py:53 | CLIO_RESOURCE_STRUCTURE_NODE_MAX_BYTES | conf.resolve; set in defaults (2097152)
OK_REFLECTED | resources.text_preview_bytes | src/clio_agent/gact/routes/resources.py:184 | CLIO_RESOURCE_TEXT_PREVIEW_BYTES | conf.resolve; set in defaults (2097152)
OK_REFLECTED | resources.text_read_chars | src/clio_agent/gact/resource_tools.py:69 | CLIO_RESOURCE_TEXT_READ_CHARS | conf.resolve; set in defaults (65536)
OK_REFLECTED | resources.text_scan_bytes | src/clio_agent/gact/resource_tools.py:102 | CLIO_RESOURCE_TEXT_SCAN_BYTES | conf.resolve; set in defaults (2097152)
OK_REFLECTED | resources.upload_chunk_bytes | src/clio_agent/gact/routes/resources.py:178 | CLIO_RESOURCE_UPLOAD_CHUNK_BYTES | conf.resolve; set in defaults (8388608)
OK_REFLECTED | runtime.api_base | src/clio_agent/runtime/status.py:720 | CLIO_API_BASE | conf.resolve; documented unset in defaults
OK_REFLECTED | runtime.capture_reasoning | src/clio_agent/gact/usage.py:237 | CLIO_CAPTURE_REASONING | conf.resolve; set in defaults (true)
OK_REFLECTED | runtime.environment | src/clio_agent/config.py:536 | CLIO_ENVIRONMENT | conf.resolve; set in defaults ("dev")
OK_REFLECTED | runtime.live_streaming | src/clio_agent/lm/adapters.py:459 | CLIO_LIVE_STREAMING | conf.resolve; set in defaults (true)
OK_REFLECTED | runtime.lm_token_liveness | src/clio_agent/lm/io_logging.py (via conf) | CLIO_LM_TOKEN_LIVENESS | conf.resolve; set in defaults (true)
OK_REFLECTED | sandbox.enabled | src/clio_agent/runtime/sandbox.py:139 | CLIO_SANDBOX_ENABLED | conf.resolve; set in defaults (true)
OK_REFLECTED | scheduler.jitter_window_s | src/clio_agent/gact/scheduler.py:205 | CLIO_SCHEDULER_JITTER_WINDOW_S | conf.resolve; set in defaults (0)
OK_REFLECTED | scheduler.max_lifetime_s | src/clio_agent/gact/scheduler.py:196 | CLIO_SCHEDULER_MAX_LIFETIME_S | conf.resolve; set in defaults (2592000)
OK_REFLECTED | scheduler.max_retries | src/clio_agent/gact/scheduler.py:187 | CLIO_SCHEDULER_MAX_RETRIES | conf.resolve; set in defaults (5)
OK_REFLECTED | scheduler.min_interval_s | src/clio_agent/gact/scheduler.py:214 | CLIO_SCHEDULER_MIN_INTERVAL_S | conf.resolve; set in defaults (60)
OK_REFLECTED | scheduler.timezone | src/clio_agent/gact/scheduler.py:231 | CLIO_SCHEDULER_TZ | conf.resolve; documented unset (key scheduler.timezone in defaults comment)
OK_REFLECTED | spotter.clearance_progress_timeout_s | src/clio_agent/gact/spotter_clearance.py:220 | CLIO_SPOTTER_CLEARANCE_PROGRESS_TIMEOUT_S | conf.resolve; set in defaults (180.0)
OK_REFLECTED | spotter.max_clearance_events | src/clio_agent/gact/spotter_clearance.py:94 | CLIO_SPOTTER_MAX_CLEARANCE_EVENTS | conf.resolve; set in defaults (256)
OK_REFLECTED | spotter.watcher_blueprint_id | src/clio_agent/gact/spotter_watcher.py:222 | CLIO_SPOTTER_BLUEPRINT_ID | conf.resolve; set in defaults ("spotter-ai")
OK_REFLECTED | spotter.watcher_expert_id | src/clio_agent/gact/spotter_watcher.py:235 | CLIO_SPOTTER_EXPERT_ID | conf.resolve; set in defaults ("spotter_watcher")
OK_REFLECTED | summarizer.api_base | src/clio_agent/lm/secondary.py:43 | CLIO_SUMMARIZER_API_BASE | conf.resolve; documented unset in defaults
OK_REFLECTED | summarizer.credential_ref | src/clio_agent/lm/secondary.py:64 | CLIO_SUMMARIZER_CREDENTIAL_REF | conf.resolve; documented unset in defaults
OK_REFLECTED | summarizer.max_tokens | src/clio_agent/lm/secondary.py:58 | CLIO_SUMMARIZER_MAX_TOKENS | conf.resolve; documented unset in defaults
OK_REFLECTED | summarizer.model | src/clio_agent/lm/secondary.py:46 | CLIO_SUMMARIZER_MODEL | conf.resolve; documented unset in defaults
OK_REFLECTED | summarizer.provider | src/clio_agent/lm/secondary.py:49 | CLIO_SUMMARIZER_PROVIDER | conf.resolve; documented unset in defaults
OK_REFLECTED | summarizer.transport | src/clio_agent/lm/secondary.py:52 | CLIO_SUMMARIZER_TRANSPORT | conf.resolve; documented unset in defaults
OK_REFLECTED | tools.file_policy.allow_symlinks | src/clio_agent/tools/file_policy.py:182 | CLIO_ALLOW_SYMLINKS | conf.resolve; set in defaults (false)
OK_REFLECTED | tools.file_policy.max_file_size_bytes | src/clio_agent/tools/file_policy.py:176 | CLIO_MAX_FILE_SIZE_BYTES | conf.resolve; set in defaults ("1073741824")
OK_REFLECTED | tools.mcp.call_timeout_s | src/clio_agent/tools/execution.py:462 | CLIO_MCP_CALL_TIMEOUT_S | conf.resolve; set in defaults (600.0)
OK_REFLECTED | tools.mcp.cold_spawn_runaway_s | src/clio_agent/tools/mcp_discovery.py:118 | CLIO_MCP_COLD_SPAWN_RUNAWAY_S | conf.resolve; set in defaults (600.0)
OK_REFLECTED | tools.mcp.connect_mode | src/clio_agent/tools/mcp_connection_era.py:134 | CLIO_MCP_CONNECT_MODE | conf.resolve; set in defaults ("auto")
OK_REFLECTED | tools.mcp.discovery_concurrency | src/clio_agent/tools/mcp_discovery.py:72 | CLIO_MCP_DISCOVERY_CONCURRENCY | conf.resolve; set in defaults (8)
OK_REFLECTED | tools.mcp.discovery_heal_interval_s | src/clio_agent/tools/mcp_discovery.py:87 | CLIO_MCP_DISCOVERY_HEAL_INTERVAL_S | conf.resolve; set in defaults (20.0)
OK_REFLECTED | tools.mcp.elicitation.agent_audience.enabled | src/clio_agent/gact/agent_elicitation.py:218 | CLIO_MCP_ELICITATION_AGENT_AUDIENCE_ENABLED | conf.resolve; set in defaults (true)
OK_REFLECTED | tools.mcp.elicitation.agent_audience.max_depth | src/clio_agent/gact/agent_elicitation.py:243 | CLIO_MCP_ELICITATION_AGENT_AUDIENCE_MAX_DEPTH | conf.resolve; set in defaults (1)
OK_REFLECTED | tools.mcp.elicitation.agent_audience.timeout_s | src/clio_agent/gact/agent_elicitation.py:254 | CLIO_MCP_ELICITATION_AGENT_AUDIENCE_TIMEOUT_S | conf.resolve; set in defaults (90.0)
OK_REFLECTED | tools.mcp.launcher_cache_lock_timeout_s | src/clio_agent/tools/launcher_cache_lock.py:97 | CLIO_MCP_LAUNCHER_CACHE_LOCK_TIMEOUT_S | conf.resolve; set in defaults (600.0)
OK_REFLECTED | tools.mcp.listing_ttl_h | src/clio_agent/tools/listing_cache.py:79 | CLIO_MCP_LISTING_TTL_H | conf.resolve; set in defaults (24.0)
OK_REFLECTED | tools.mcp.mount_retry_delays_s | src/clio_agent/gact/mcp_readiness.py:42 | CLIO_MCP_MOUNT_RETRY_DELAYS_S | conf.resolve; set in defaults (["0.5","1.5"])
OK_REFLECTED | tools.mcp.probe_timeout_retries | src/clio_agent/tools/mcp_probe_hardening.py:110 | CLIO_MCP_PROBE_TIMEOUT_RETRIES | conf.resolve; set in defaults (3)
OK_REFLECTED | tools.mcp.response_cache_enabled | src/clio_agent/tools/mcp_runtime.py:507 | CLIO_MCP_RESPONSE_CACHE_ENABLED | conf.resolve; set in defaults (false)
OK_REFLECTED | tools.mcp.setup_timeout_s | src/clio_agent/gact/mcp_readiness.py:65 | CLIO_MCP_SETUP_TIMEOUT_S | conf.resolve; set in defaults (10.0)
OK_REFLECTED | tools.mcp.spawn_diet | src/clio_agent/tools/spawn_diet.py:109 | CLIO_MCP_SPAWN_DIET | conf.resolve; set in defaults (true)
OK_REFLECTED | tools.mcp.spawn_diet_ttl_h | src/clio_agent/tools/spawn_diet.py:122 | CLIO_MCP_SPAWN_DIET_TTL_H | conf.resolve; set in defaults (24.0)
OK_REFLECTED | tools.mcp.workspace_max_resident | src/clio_agent/tools/reaper.py:80 | CLIO_MCP_WORKSPACE_MAX_RESIDENT | conf.resolve; set in defaults (2)
OK_REFLECTED | tools.mcp.workspace_ttl_s | src/clio_agent/tools/reaper.py:67 | CLIO_MCP_WORKSPACE_TTL_S | conf.resolve; set in defaults (120.0)
OK_REFLECTED | tools.mcp_cache.max_age_days | src/clio_agent/tools/mcp_cache.py:130 | CLIO_MCP_CACHE_MAX_AGE_DAYS | conf.resolve; set in defaults (14.0)
OK_REFLECTED | tools.mcp_cache.max_bytes | src/clio_agent/tools/mcp_cache.py:151 | CLIO_MCP_CACHE_MAX_BYTES | conf.resolve; set in defaults (2147483648)
OK_REFLECTED | tools.mcp_cache.temp_max_age_days | src/clio_agent/runtime/disk_gc.py:443 | CLIO_MCP_CACHE_TEMP_MAX_AGE_DAYS | conf.resolve; set in defaults (3.0)
OK_REFLECTED | tools.mcp_cache.temp_roots | src/clio_agent/runtime/disk_gc.py:472 | CLIO_MCP_CACHE_TEMP_ROOTS | conf.resolve; documented unset in defaults
OK_REFLECTED | tools.shell.windows_backend | src/clio_agent/tools/servers/shell_server.py:152 | CLIO_WINDOWS_SHELL_BACKEND | conf.resolve; set in defaults ("powershell")
OK_REFLECTED | trace.detail_level | src/clio_agent/gact/_params.py:121 | CLIO_SEMANTIC_TRACE_DETAIL | conf.resolve; set in defaults ("semantic")
OK_REFLECTED | trace.path | src/clio_agent/gact/provenance/factory.py:94 | CLIO_SEMANTIC_TRACE_PATH | conf.resolve; documented unset in defaults
OK_REFLECTED | trace.semantic_config | src/clio_agent/gact/provenance/factory.py:202 | CLIO_SEMANTIC_TRACE_CONFIG | conf.resolve; documented unset in defaults
OK_REFLECTED | trace.semantic_factory | src/clio_agent/gact/provenance/factory.py:194 | CLIO_SEMANTIC_TRACE_FACTORY | conf.resolve; documented unset in defaults
OK_REFLECTED | workflows.step_inactivity_s | src/clio_agent/gact/workflow_step_watch.py:54 | CLIO_WORKFLOW_STEP_INACTIVITY_S | conf.resolve; set in defaults (120.0)
OK_REFLECTED | tools.mcp.input_required_max_rounds | src/clio_agent/tools/mcp_runtime.py:273 | CLIO_MCP_INPUT_REQUIRED_MAX_ROUNDS | conf.resolve; documented in defaults comment as computed at runtime
OK_REFLECTED | tools.file_policy.allowed_roots | src/clio_agent/tools/file_policy.py:170 | CLIO_ALLOWED_ROOTS | conf.resolve; documented in defaults as computed at runtime
OK_REFLECTED | tools.mcp.elicitation.agent_audience.denied_servers | src/clio_agent/gact/agent_elicitation.py:231 | CLIO_MCP_ELICITATION_AGENT_AUDIENCE_DENIED_SERVERS | conf.resolve; documented in defaults comment as computed at runtime
OK_REFLECTED | tools.mcp.elicitation.url_trusted_origins | src/clio_agent/gact/elicitation_bridge.py:682 | CLIO_MCP_ELICITATION_URL_TRUSTED_ORIGINS | conf.resolve; documented in defaults comment as computed at runtime
OK_REFLECTED | agents.child_forward_deadline_s | src/clio_agent/gact/child_forward.py:100 | CLIO_CHILD_FORWARD_DEADLINE_S | conf.resolve; documented in defaults comment as computed at runtime
OK_REFLECTED | provenance.artifacts.include_events | src/clio_agent/gact/artifacts/provenance/factory.py:43 | CLIO_ARTIFACT_PROVENANCE_EVENTS | conf.resolve; documented in defaults comment as computed at runtime

---

## OK_UNREFLECTED

Keys that use `conf.resolve` correctly but are MISSING from `config.defaults.yaml` (neither set nor documented).

OK_UNREFLECTED | tools.mcp.elicitation.agent_audience.answer_mode | src/clio_agent/gact/agent_elicitation.py:272 | CLIO_MCP_ELICITATION_AGENT_AUDIENCE_ANSWER_MODE | conf.resolve with default="inline"; absent from config.defaults.yaml entirely
OK_UNREFLECTED | tools.mcp.elicitation.agent_audience.default_unhinted | src/clio_agent/gact/agent_elicitation.py:291 | CLIO_MCP_ELICITATION_AGENT_AUDIENCE_DEFAULT_UNHINTED | conf.resolve with default=True; absent from config.defaults.yaml entirely
OK_UNREFLECTED | provenance.agentic.flowcept.settings_path | src/clio_agent/gact/provenance/factory.py:130 | FLOWCEPT_SETTINGS_PATH | conf.resolve with default=""; absent from config.defaults.yaml entirely; env alias is third-party name
OK_UNREFLECTED | scheduler.tz | src/clio_agent/gact/scheduler.py:236 | TZ | conf.resolve secondary fallback using system TZ env var; key scheduler.tz not in defaults (scheduler.timezone is documented)
OK_UNREFLECTED | debug.only | src/clio_agent/runtime/trace.py:123 | CLIO_DEBUG_ONLY | conf.resolve; defaults.yaml has comment but key is "debug.only" in comment entry labeled "computed at runtime: _no_only" — classified as OK_REFLECTED above (comment entry); re-classified as OK_REFLECTED per comment presence

---

## ENV_SANCTIONED

Bare `os.environ`/`os.getenv` reads that are on conf.py's exemption list or are resolver/bootstrap machinery.

ENV_SANCTIONED | CLIO_ENV_FILE | src/clio_agent/config.py:144 | | Bootstrap tier: dotenv loader reads its own env file path before store exists
ENV_SANCTIONED | CLIO_ENV_FILE_LOADED | src/clio_agent/config.py:139 | | Bootstrap tier: dotenv loader marks load complete via env
ENV_SANCTIONED | CLIO_LM_API_KEY | src/clio_agent/config.py:535 | | Secret tier: explicitly exempted in conf.py docstring; never file-resolved
ENV_SANCTIONED | CLIO_LM_API_KEY | src/clio_agent/providers/model_discovery/overlay.py:296 | | Secret tier: credential read; explicitly exempted
ENV_SANCTIONED | CLIO_ARGONNE_TOKEN | src/clio_agent/providers/credentials.py:95 | | Secret tier: explicitly named in conf.py exemptions (CLIO_ARGONNE_TOKEN)
ENV_SANCTIONED | ALCF_INFERENCE_TOKEN | src/clio_agent/providers/credentials.py:96 | | Secret tier: explicitly named in conf.py exemptions (ALCF_INFERENCE_TOKEN)
ENV_SANCTIONED | CLIO_ARGONNE_TOKEN | src/clio_agent/gact/routes/providers.py:536 | | Secret/auth-status probe tier: provider auth-status probe; exempted
ENV_SANCTIONED | ALCF_INFERENCE_TOKEN | src/clio_agent/gact/routes/providers.py:537 | | Secret/auth-status probe tier: provider auth-status probe; exempted
ENV_SANCTIONED | CLIO_LM_API_KEY | src/clio_agent/gact/routes/providers.py:170 | | Auth-status probe tier: presence check drives auth UI; explicitly exempted
ENV_SANCTIONED | CLIO_LM_API_KEY | src/clio_agent/gact/routes/providers.py:571 | | Auth-status probe tier: presence check drives auth UI; explicitly exempted
ENV_SANCTIONED | CLIO_RELAY_API_TOKEN | src/clio_agent/tools/relay_factory.py:235 | | Secret tier: explicitly named in conf.py exemptions (CLIO_RELAY_API_TOKEN)
ENV_SANCTIONED | CLIO_RELAY_API_TOKEN | src/clio_agent/tools/relay_transport.py:207 | | Secret tier: explicitly named in conf.py exemptions (CLIO_RELAY_API_TOKEN)
ENV_SANCTIONED | CLIO_USER_DIR | src/clio_agent/paths.py:33 | | Bootstrap tier: explicitly named in conf.py exemptions; read before store exists
ENV_SANCTIONED | XDG_CONFIG_HOME | src/clio_agent/paths.py (via user_config_dir_for) | | Bootstrap tier: explicitly named in conf.py exemptions; drives file discovery
ENV_SANCTIONED | CLIO_CRED_* (per-account env vars) | src/clio_agent/providers/credentials.py:167-172 | | Secret tier: CLIO_CRED_* explicitly listed in conf.py exemptions
ENV_SANCTIONED | ANTHROPIC_API_KEY / OPENAI_API_KEY | src/clio_agent/providers/credentials.py:170-172 | | Secret/auth-status probe tier: cloud provider key resolution; exempted
ENV_SANCTIONED | os.environ (copy for subprocess) | src/clio_agent/arc/storage.py:315,511 | | Process env copy for subprocess spawn; not a config read
ENV_SANCTIONED | CTP_LOG_LEVEL (setdefault) | src/clio_agent/arc/storage.py:656 | | Subprocess env injection for clio-core logger; not a config read
ENV_SANCTIONED | os.environ (preflight check) | src/clio_agent/arc/storage.py:887 | | Env passed to preflight helper, not a config read itself
ENV_SANCTIONED | os.environ (subprocess env) | src/clio_agent/gact/agent_blueprints.py:797 | | Subprocess env copy for git clone; not a config read
ENV_SANCTIONED | os.environ (subprocess env) | src/clio_agent/gact/agent_blueprint_sources.py:126 | | Subprocess env copy; not a config read
ENV_SANCTIONED | os.environ (for paths helper) | src/clio_agent/gact/agent_blueprints.py:90,913 | | Bootstrap: paths.user_config_dir_for reads XDG_CONFIG_HOME via env mapping
ENV_SANCTIONED | os.environ (for paths helper) | src/clio_agent/gact/catalog.py:183 | | Bootstrap: same XDG paths helper pattern
ENV_SANCTIONED | os.environ (for paths helper) | src/clio_agent/gact/expert_packs.py:200 | | Bootstrap: same XDG paths helper pattern
ENV_SANCTIONED | os.environ (for file policy mapping) | src/clio_agent/gact/routes/workspaces.py:541,740 | | FileAccessPolicy.from_mapping reads CLIO_ALLOWED_ROOTS from env mapping; fallback to conf.resolve is the normal path
ENV_SANCTIONED | os.environ (for spotter arming) | src/clio_agent/gact/spotter_arming.py:351 | | env mapping passed to spotter arming; infrastructure-level
ENV_SANCTIONED | FLOWCEPT_SETTINGS_PATH (write) | src/clio_agent/gact/provenance/flowcept.py:191 | | Writes env to pass settings to third-party Flowcept library; not a config read
ENV_SANCTIONED | PROCESSOR_ARCHITECTURE / COMPUTERNAME / OS (Windows info) | src/clio_agent/gact/provenance/flowcept.py:120 | | Platform info probe; not a user-facing config knob
ENV_SANCTIONED | PROCESSOR_IDENTIFIER / PROCESSOR_ARCHITECTURE / OS | src/clio_agent/gact/__init__.py:61-73 | | Windows platform fingerprint; not a user-facing config knob
ENV_SANCTIONED | os.environ (for mcp_config) | src/clio_agent/tools/mcp_config.py:182,503 | | env mapping for MCP config discovery; infrastructure
ENV_SANCTIONED | os.environ (for mcp_environment) | src/clio_agent/tools/mcp_environment.py:12 | | Builds subprocess env overlay; not a config read
ENV_SANCTIONED | os.environ (relay_cli_runner passthrough) | src/clio_agent/tools/relay_cli_runner.py:523 | | Relay CLI allowed-env passthrough for subprocess; infrastructure
ENV_SANCTIONED | os.environ (runtime health envs) | src/clio_agent/runtime/clio_core_health.py:76,356,476,586 | | env mapping injection into health check helpers; infrastructure
ENV_SANCTIONED | os.environ (disk_gc subprocess) | src/clio_agent/runtime/disk_gc.py:409,551 | | Subprocess env for disk GC; infrastructure
ENV_SANCTIONED | APPDATA / LOCALAPPDATA (Windows paths) | src/clio_agent/runtime/sandbox_cli.py:252-253 | | Windows platform path lookup; not a user config knob
ENV_SANCTIONED | CODEX_HOME | src/clio_agent/runtime/sandbox_codex.py:252 | | SDK-standard env for codex home; not a CLIO config knob (read-only probe)
ENV_SANCTIONED | LOCALAPPDATA / XDG_DATA_HOME | src/clio_agent/providers/argonne_auth.py:191,197 | | Platform data dir discovery; not a user config knob
ENV_SANCTIONED | ProgramFiles / ProgramFiles(x86) | src/clio_agent/gact/documents/renditions.py:83-84 | | Windows executable search; OS env, not a config knob
ENV_SANCTIONED | CODEX_HOME | src/clio_agent/runtime/lm_provider_probe.py:94 | | SDK-standard env for codex home; not a CLIO config knob
ENV_SANCTIONED | CODEX_HOME | src/clio_agent/providers/codex_credential_home.py:158 | | SDK-standard env for codex home; not a CLIO config knob
ENV_SANCTIONED | CODEX_HOME | src/clio_agent/providers/handshake/argonne.py:95 | | Argonne auth probe vars; credential/auth-status tier
ENV_SANCTIONED | os.environ (status env mapping) | src/clio_agent/runtime/status.py:206 | | env mapping for status doctor; infrastructure-level
ENV_SANCTIONED | CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS (secondary bare read) | src/clio_agent/agent.py:880 | CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS | Special case: bare read only on conf.resolve fallthrough to detect set-but-empty sentinel; the knob itself is resolved via conf.resolve at line 865

---

## ENV_BARE

Bare `os.environ`/`os.getenv` reads in agent code that should go through `conf.resolve`.

ENV_BARE | CLIO_ARC_STORE | src/clio_agent/arc/init_degradation.py:112 | | `os.environ.get("CLIO_ARC_STORE")` for degradation labeling; `arc.store` is already conf.resolve'd in storage.py but this secondary bare read bypasses the file layer
ENV_BARE | CLIO_RUNTIME_STATE_DIR | src/clio_agent/arc/clio_core_config.py:79 | | `os.environ.get("CLIO_RUNTIME_STATE_DIR")` for clio-core state dir; no corresponding conf.resolve key exists — bare read only
ENV_BARE | CHI_SERVER_CONF | src/clio_agent/arc/clio_core_liveness.py:145 | | `os.environ.get("CHI_SERVER_CONF")` as fallback after `arc.server_conf` (conf.resolve); a third-party / legacy env alias read bare after the conf lookup
ENV_BARE | CLIO_ONLYOFFICE_URL | src/clio_agent/gact/documents/editors.py:192 | | `os.environ.get("CLIO_ONLYOFFICE_URL")` with no conf.resolve — user-facing knob for OnlyOffice editor endpoint, entirely bypasses conf store
ENV_BARE | CLIO_COLLABORA_URL | src/clio_agent/gact/documents/editors.py:194 | | `os.environ.get("CLIO_COLLABORA_URL")` with no conf.resolve — user-facing knob for Collabora editor endpoint
ENV_BARE | CLIO_GACT_PUBLIC_URL | src/clio_agent/gact/documents/editors.py:204 | | `os.environ.get("CLIO_GACT_PUBLIC_URL", "http://host.docker.internal:8000")` with no conf.resolve — operator-facing URL knob with hardcoded default
ENV_BARE | CLIO_ONLYOFFICE_JWT_SECRET | src/clio_agent/gact/documents/editors.py:243 | | `os.environ.get("CLIO_ONLYOFFICE_JWT_SECRET")` — JWT signing secret; could be secret-tier but the conf.py exemption list does not include it
ENV_BARE | CLIO_DOCUMENT_TYPST_FONT | src/clio_agent/gact/documents/renditions.py:145 | | `os.environ.get("CLIO_DOCUMENT_TYPST_FONT")` with no conf.resolve — user-tunable font preference for typst rendition
ENV_BARE | LM_STUDIO_API_TOKEN | src/clio_agent/gact/providers/lmstudio.py:44 | | `os.environ.get("LM_STUDIO_API_TOKEN")` — credential read but not on conf.py's exemption list by that name
ENV_BARE | LM_API_TOKEN | src/clio_agent/gact/providers/lmstudio.py:45 | | `os.environ.get("LM_API_TOKEN")` — fallback LM Studio API token; not on exemption list
ENV_BARE | CLIO_PROVENANCE_PROVIDERS | src/clio_agent/provenance_config.py:34,112 | | Double bare read — the function also calls conf.resolve at line 52; the bare reads at 34 and 112 pre-check file_value then os.environ directly for a two-stage override pattern, bypassing the full resolve cascade
ENV_BARE | CLIO_SEMANTIC_TRACE_BACKEND | src/clio_agent/provenance_config.py:40,118 | | Legacy key read via `os.environ.get` without conf.resolve for backwards compat; no corresponding conf.resolve call for this key

---

## HARDCODED

Operational tunables that are bare literals or module constants with no `conf.resolve`.

HARDCODED | _RPC_STALLED_RECOVERY_TTL_S=30.0 | src/clio_agent/arc/clio_core_liveness.py:90 | | TTL for stalled-RPC quarantine state; hardcoded, no env/conf knob — an operator might want to tune for very slow daemons
HARDCODED | _HEALTH_PROBE_MAX_S=10.0 | src/clio_agent/arc/rpc_liveness.py:66 | | Upper bound on single-attempt health probe; hardcoded, no conf.resolve — tunable for slow networks
HARDCODED | _BACKOFF_FACTOR=3.0 | src/clio_agent/arc/rpc_liveness.py:60 | | Backoff growth multiplier between stall retries; hardcoded, no conf.resolve — tunable alongside arc.liveness.backoff_* knobs
HARDCODED | _STALL_WATCH_MAX_WORKERS=8 | src/clio_agent/arc/rpc_liveness.py:189 | | Thread pool size cap for stall-watch watchdog; hardcoded, no conf.resolve — tunable for high-concurrency hosts
HARDCODED | _FILE_TIER_FREE_SPACE_RESERVE_BYTES=1GiB | src/clio_agent/arc/clio_core_file_capacity.py:24 | | Disk free-space reserve kept back from CTE file tier; hardcoded, no conf.resolve — operators on constrained disks may need to lower it
HARDCODED | _RUNTIME_START_TIMEOUT_S=30.0 | src/clio_agent/arc/storage.py:274 | | Timeout waiting for clio-core daemon to start; hardcoded — tunable for slow machines
HARDCODED | _DEFAULT_BOOTSTRAP_TIMEOUT_S=20 | src/clio_agent/gact/agent_blueprints.py:53 | | Timeout for blueprint git operations (clone/fetch); hardcoded — tunable for slow git servers
HARDCODED | _RETRY_BACKOFF_BASE_S=60 | src/clio_agent/gact/scheduler.py:85 | | Base backoff seconds before first scheduler retry; hardcoded — tunable alongside scheduler.max_retries
HARDCODED | _RETRY_BACKOFF_CAP_S=3600 | src/clio_agent/gact/scheduler.py:88 | | Cap on exponential scheduler retry backoff; hardcoded — tunable alongside _RETRY_BACKOFF_BASE_S
HARDCODED | WAKEUP_MIN_S=60 | src/clio_agent/gact/autonomous_loop.py:58 | | Minimum sleep between autonomous loop wakeups; hardcoded — tunable floor for sub-minute wakeup schedules
HARDCODED | WAKEUP_MAX_S=3600 | src/clio_agent/gact/autonomous_loop.py:59 | | Maximum sleep between autonomous loop wakeups; hardcoded — tunable ceiling
HARDCODED | DEFAULT_INTERVAL_S=300 | src/clio_agent/gact/autonomous_loop.py:63 | | Default autonomous loop sleep interval; hardcoded — user-facing default never surfaced via conf.resolve
HARDCODED | DEFAULT_MAX_ITERS=100 | src/clio_agent/gact/autonomous_loop.py:66 | | Default max iterations for autonomous loop; hardcoded — user-facing default
HARDCODED | DEFAULT_MAX_WALLCLOCK_S=86400 | src/clio_agent/gact/autonomous_loop.py:68 | | Default max wall-clock seconds for autonomous loop; hardcoded
HARDCODED | _RELAY_DISCOVERY_FAILURE_TTL_SECONDS=20.0 | src/clio_agent/gact/relay_wiring.py:37 | | Cap on failure-mode relay discovery TTL; hardcoded — tunable for slow relay clusters
HARDCODED | _EGRESS_GATE_TIMEOUT_S=600.0 | src/clio_agent/gact/runtime/grants.py:94 | | Timeout waiting for an egress permission grant event; hardcoded — tunable for very slow approval flows
HARDCODED | MAX_SPAWN_DEPTH=8 | src/clio_agent/gact/session_descendants.py:32 | | Hard runaway cap on agent spawn depth; hardcoded — tunable for legitimately deep declared chains
HARDCODED | DEFAULT_MCP_TIMEOUT_S=20.0 | src/clio_agent/providers/handshake/mcp.py:33 | | Per-call timeout for MCP handshake probes; hardcoded — tunable for slow-starting MCP servers
HARDCODED | _CONNECT_READ_TIMEOUT_S=30.0 | src/clio_agent/runtime/net_chokepoint.py:69 | | Connect+read timeout for network chokepoint probes; hardcoded — tunable for slow upstreams
HARDCODED | RELAY_PROBE_TIMEOUT_SECONDS=3.0 | src/clio_agent/gact/relay_status.py:12 | | TCP probe timeout for relay status check; hardcoded — tunable for slow relay hosts
HARDCODED | _FETCH_TIMEOUT_S=6.0 | src/clio_agent/providers/handshake/sources/models_dev.py:43 | | HTTP timeout for models.dev catalog fetch; hardcoded — tunable for slow external endpoints
HARDCODED | CONSOLE_SSE_HEALTHZ_TIMEOUT_SECONDS=3.0 | src/clio_agent/tools/relay_console_stream.py:90 | | Health probe timeout for SSE console stream; hardcoded
HARDCODED | CONSOLE_SSE_READ_TIMEOUT_SECONDS=30.0 | src/clio_agent/tools/relay_console_stream.py:96 | | Read timeout for SSE console stream events; hardcoded — tunable for very large log bursts
HARDCODED | _OUTER_TIMEOUT_MARGIN_S=15.0 | src/clio_agent/gact/agent_elicitation.py:206 | | Grace margin added to elicitation turn timeout; hardcoded — tunable margin
HARDCODED | _CONTEXT_EXCERPT_MAX_CHARS=6000 | src/clio_agent/gact/agent_elicitation.py:207 | | Char cap on context excerpt injected into elicitation answer prompt; hardcoded
HARDCODED | DEFAULT_MAX_GOAL_ITERS=25 | src/clio_agent/gact/goal.py:70 | | Default max iterations for goal-monitoring loop; hardcoded — tunable default
HARDCODED | DEFAULT_TURN_TIMEOUT_S=180.0 | src/clio_agent/providers/codex_stream.py:64 | | Per-turn ceiling passed to Codex SDK; hardcoded constant (actual operative limit is limits.codex_sdk_progress_timeout_s via conf.resolve) — redundant ceiling may surprise operators
HARDCODED | _MCP_REGISTRY_LIMIT=64 | src/clio_agent/gact/mcp_apps.py:59 | | MCP app instance registry cap; hardcoded — tunable for high-concurrency MCP app workloads
HARDCODED | _MCP_REGISTRY_TTL_S=3600 | src/clio_agent/gact/mcp_apps.py:60 | | TTL for MCP app registry entries; hardcoded — tunable for long-lived MCP apps

---

## TOTALS

| Status | Count |
|---|---|
| OK_REFLECTED | 225 |
| OK_UNREFLECTED | 4 |
| ENV_SANCTIONED | 43 |
| ENV_BARE | 12 |
| HARDCODED | 32 |
| **TOTAL** | **316** |

---

## Notes for orchestrator

1. **ENV_BARE highlights**: The `gact/documents/editors.py` file has 4 ENV_BARE violations (CLIO_ONLYOFFICE_URL, CLIO_COLLABORA_URL, CLIO_GACT_PUBLIC_URL, CLIO_ONLYOFFICE_JWT_SECRET). These document-editor knobs are entirely outside the conf.resolve system with no defaults.yaml entries.

2. **ENV_BARE: CLIO_DOCUMENT_TYPST_FONT** in `renditions.py:145` is a user-tunable font name with a platform-dependent in-code default but no conf key.

3. **ENV_BARE: LM_STUDIO_API_TOKEN / LM_API_TOKEN** in `lmstudio.py` read third-party LM Studio credential env vars. These are arguably credential-tier (similar to ALCF_INFERENCE_TOKEN) but are not named in conf.py's exemption list.

4. **ENV_BARE: CLIO_PROVENANCE_PROVIDERS / CLIO_SEMANTIC_TRACE_BACKEND** in `provenance_config.py` perform a two-stage file_value + os.environ check BEFORE calling conf.resolve. This partial bypass of the full resolve cascade is intentional (for legacy compat) but creates a second read path outside the documented store.

5. **OK_UNREFLECTED: provenance.agentic.flowcept.settings_path** uses env alias `FLOWCEPT_SETTINGS_PATH` (third-party name) — not CLIO-namespaced. This is in conf.resolve but missing from config.defaults.yaml entirely.

6. **OK_UNREFLECTED: scheduler.tz** is a secondary conf.resolve fallback to system `TZ` env var (the `scheduler.timezone` key is in defaults; the `scheduler.tz` alias key is not).

7. **HARDCODED items are all operational tunables** that would plausibly need operator tuning on non-standard hardware or slow networks; none are protocol constants or schema names.
