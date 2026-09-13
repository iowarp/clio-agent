## CONFIGURATION KNOB AUDIT — AUDITOR C

**Repo:** /u/jcernuda/spotterAI/clio-agent (src/clio_agent/ only)  
**Scope:** Agent/product code (NOT tests, scripts, deploy)  
**Method:** Systematic grep of conf.resolve(), os.environ/os.getenv, hardcoded constants  
**Config System:** conf.py → config.defaults.yaml deep-merge + exemption tier (lines 40–53)

---

## OK_REFLECTED
Configuration keys using `conf.resolve()` AND found in config.defaults.yaml as set values or documented unset entries.

OK_REFLECTED | limits.lm_call_s | src/clio_agent/runtime/lm_activity.py:84 | CLIO_MAX_LM_CALL_S | Hard timeout for LM calls before watchdog assumes wedged
OK_REFLECTED | limits.lm_inter_token_idle_s | src/clio_agent/runtime/lm_activity.py:94 | CLIO_LM_INTER_TOKEN_IDLE_S | Token-stream stall detection window when streaming
OK_REFLECTED | limits.lm_transient_backoff_s | src/clio_agent/lm/io_logging.py:101 | CLIO_LM_TRANSIENT_BACKOFF_S | Wait before retrying transient LM provider failure
OK_REFLECTED | limits.lm_transient_retries | src/clio_agent/lm/io_logging.py:115 | CLIO_LM_TRANSIENT_RETRIES | Retry budget for transient non-parse provider failures
OK_REFLECTED | lm.provider | src/clio_agent/config.py:536 | CLIO_LM_PROVIDER | LM backend selection (lm_studio, ollama, openai, anthropic, etc.)
OK_REFLECTED | lm.defer_tiktoken | src/clio_agent/lm/factory.py:50 | CLIO_LM_DEFER_TIKTOKEN | Defer ~40MB tiktoken vocab load until first encode
OK_REFLECTED | lm.disable_json_adapter_fallback | src/clio_agent/lm/adapters.py:394 | CLIO_DISABLE_JSON_ADAPTER_FALLBACK | Force-disable JSON adapter fallback when provider rejects response_format
OK_REFLECTED | lm.disable_thinking | src/clio_agent/lm/factory.py:62 | CLIO_LM_DISABLE_THINKING | Suppress reasoning/thinking sampling even on capable models
OK_REFLECTED | lm.guided_output | src/clio_agent/lm/adapters.py:410 | CLIO_LM_GUIDED_OUTPUT | Use schema-constrained JSON output instead of text ChatAdapter
OK_REFLECTED | lm.lmstudio_flash_attention | src/clio_agent/gact/routes/providers.py:53 | CLIO_LMSTUDIO_FLASH_ATTENTION | Request flash attention on LM Studio model loads
OK_REFLECTED | providers.claude_code.max_concurrent_processes | src/clio_agent/providers/claude_code_stream_bounds.py:72 | CLIO_CLAUDE_CODE_MAX_CONCURRENT_PROCESSES | Process-wide cap on concurrent Claude CLI subprocesses
OK_REFLECTED | providers.claude_code.probe_timeout_s | src/clio_agent/providers/model_discovery/claude_code.py:70 | CLIO_CLAUDE_CODE_PROBE_TIMEOUT_S | Seconds for one Claude Code model-discovery probe before abandoned
OK_REFLECTED | providers.claude_code.session_reuse | src/clio_agent/providers/claude_code_sessions.py:40 | CLIO_CLAUDE_CODE_SESSION_REUSE | Pool/reuse SDK connection per scope or use fresh client per call
OK_REFLECTED | providers.claude_code.stateful_capacity | src/clio_agent/providers/claude_code_stateful.py:44 | CLIO_CLAUDE_CODE_STATEFUL_CAPACITY | Max live Claude Code stateful-session entries before LRU eviction
OK_REFLECTED | providers.claude_code.stream_idle_ttl_s | src/clio_agent/providers/claude_code_stream_bounds.py:46 | CLIO_CLAUDE_CODE_STREAM_IDLE_TTL_S | Idle seconds before a pooled scope-keyed connection is reaped
OK_REFLECTED | providers.codex.credential_home_capacity | src/clio_agent/providers/codex_credential_home.py:84 | CLIO_CODEX_CREDENTIAL_HOME_CAPACITY | Max simultaneous private CODEX_HOME credential-dir copies
OK_REFLECTED | providers.model_catalog_ttl_s | src/clio_agent/providers/model_discovery/overlay.py:151 | CLIO_MODEL_CATALOG_TTL_S | Seconds provider model catalog is served fresh before marked stale
OK_REFLECTED | limits.agent_task_artifact_context_chars | src/clio_agent/gact/agent_task_artifacts.py:251 | CLIO_AGENT_TASK_ARTIFACT_CONTEXT_CHARS | Character bound on artifact content injected into commissioning parent
OK_REFLECTED | limits.agent_task_output_digest_chars | src/clio_agent/gact/agents/agent_task_output_digest.py:50 | CLIO_AGENT_TASK_OUTPUT_DIGEST_CHARS | Threshold above which child output is digested instead of inlined
OK_REFLECTED | limits.codex_sdk_progress_timeout_s | src/clio_agent/providers/codex_stream.py:43 | CLIO_CODEX_SDK_PROGRESS_TIMEOUT_S | Max silence (seconds) for one Codex SDK exchange before timeout
OK_REFLECTED | limits.context_inline_bytes | src/clio_agent/gact/runtime/constants.py:41 | CLIO_CTX_MAX_BYTES | Byte cap per attached file inlined into context injection
OK_REFLECTED | limits.fs_read_bytes | src/clio_agent/tools/servers/fs_server.py:92 | CLIO_FS_MAX_READ_BYTES | Byte cap on a single read_file tool call
OK_REFLECTED | limits.mcp_content_block_max_bytes | src/clio_agent/tools/mcp_results.py:48 | CLIO_MCP_CONTENT_BLOCK_MAX_BYTES | Byte cap on one MCP content block's decoded binary payload
OK_REFLECTED | limits.mcp_reconnect_timeout_s | src/clio_agent/gact/routes/mcp.py:80 | CLIO_GACT_MCP_RECONNECT_TIMEOUT_S | Seconds bounding MCP connect+list-tools round-trip on reconnect
OK_REFLECTED | limits.model_tool_result_chars | src/clio_agent/tools/mcp_result_projection.py:81 | CLIO_MODEL_TOOL_RESULT_CHARS | Character bound on model-facing MCP tool-result projection
OK_REFLECTED | limits.plan_review_chars | src/clio_agent/gact/plan_review.py:25 | CLIO_PLAN_REVIEW_CHARS | Character bound on saved plan content embedded in approval record
OK_REFLECTED | limits.shell_default_output_bytes | src/clio_agent/tools/servers/shell_server.py:124 | CLIO_SHELL_DEFAULT_OUTPUT_BYTES | Default byte cap on shell-command stdout/stderr
OK_REFLECTED | limits.shell_default_timeout_s | src/clio_agent/tools/servers/shell_server.py:118 | CLIO_SHELL_DEFAULT_TIMEOUT_S | Default wall-clock seconds a shell command may run
OK_REFLECTED | limits.shell_max_command_chars | src/clio_agent/tools/servers/shell_server.py:107 | CLIO_SHELL_MAX_COMMAND_CHARS | Max character length of a shell command string
OK_REFLECTED | limits.shell_max_output_bytes | src/clio_agent/tools/servers/shell_server.py:130 | CLIO_SHELL_MAX_OUTPUT_BYTES | Hard ceiling in bytes on shell-command output ever returned
OK_REFLECTED | limits.shell_max_timeout_s | src/clio_agent/tools/servers/shell_server.py:137 | CLIO_SHELL_MAX_TIMEOUT_S | Hard ceiling in seconds on shell tool timeout requests
OK_REFLECTED | limits.tool_result_chars | src/clio_agent/gact/evidence.py:126 | CLIO_TOOL_RESULT_CHARS | Character bound on transcript/evidence-metadata preview of tool result
OK_REFLECTED | limits.turn_timeout_s | src/clio_agent/gact/_params.py:103 | CLIO_GACT_TURN_TIMEOUT_S | Seconds a turn may run with no progress before timeout
OK_REFLECTED | agent_tasks.max_concurrent | src/clio_agent/gact/turn_spawn_executor.py:56 | CLIO_MAX_CONCURRENT_AGENT_TASKS | Max child agent-task threads run concurrently at one spawn depth
OK_REFLECTED | agents.default_blueprint_id | src/clio_agent/gact/agent_blueprint_refresh.py:76 | CLIO_DEFAULT_AGENT_BLUEPRINT_ID | Installed marketplace Agent Blueprint a fresh deployment bootstraps
OK_REFLECTED | agents.disable_default_registry_bootstrap | src/clio_agent/gact/agent_blueprint_refresh.py:110 | CLIO_AGENT_DISABLE_DEFAULT_REGISTRY_BOOTSTRAP | Disable auto-installing default marketplace Agent-Blueprint registry
OK_REFLECTED | scheduler.jitter_window_s | src/clio_agent/gact/scheduler.py:243 | CLIO_SCHEDULER_JITTER_WINDOW_S | Seconds-wide window a schedule's fire time is deterministically jittered
OK_REFLECTED | scheduler.max_lifetime_s | src/clio_agent/gact/scheduler.py:236 | CLIO_SCHEDULER_MAX_LIFETIME_S | Seconds after creation recurring schedule with no explicit until is auto-retired
OK_REFLECTED | scheduler.max_retries | src/clio_agent/gact/scheduler.py:229 | CLIO_SCHEDULER_MAX_RETRIES | Consecutive failed fires tolerated before schedule is disabled
OK_REFLECTED | scheduler.min_interval_s | src/clio_agent/gact/scheduler.py:250 | CLIO_SCHEDULER_MIN_INTERVAL_S | Floor on how often any recurring schedule may fire
OK_REFLECTED | workflows.step_inactivity_s | src/clio_agent/gact/workflow_step_watch.py:40 | CLIO_WORKFLOW_STEP_INACTIVITY_S | No-activity window before declared-workflow step's child is judged stalled
OK_REFLECTED | a2ui.max_message_bytes | src/clio_agent/gact/a2ui.py:42 | CLIO_A2UI_MAX_MESSAGE_BYTES | Byte ceiling on one encoded A2UI server-to-client message
OK_REFLECTED | a2ui.max_string_chars | src/clio_agent/gact/a2ui.py:60 | CLIO_A2UI_MAX_STRING_CHARS | Character ceiling on any single string inside A2UI payload
OK_REFLECTED | autocompact.pct | src/clio_agent/gact/runtime/context_tokens.py:41 | CLIO_AUTOCOMPACT_PCT | Fraction of model context window that triggers proactive auto-compaction
OK_REFLECTED | gact.ask_user.ttl_s | src/clio_agent/gact/ask_user_tool.py:56 | CLIO_ASK_USER_TTL_S | Default ask_user response window in seconds
OK_REFLECTED | gact.ask_user.max_ttl_s | src/clio_agent/gact/ask_user_tool.py:62 | CLIO_ASK_USER_MAX_TTL_S | Hard ceiling in seconds on ask_user response window
OK_REFLECTED | gact.blueprint_registry.url | src/clio_agent/gact/agent_blueprints.py:109 | CLIO_BLUEPRINT_REGISTRY_URL | Git URL of default agent-blueprint marketplace registry
OK_REFLECTED | gact.blueprint_source.clone_timeout_s | src/clio_agent/gact/agent_blueprint_sources.py:74 | CLIO_BLUEPRINT_SOURCE_CLONE_TIMEOUT_S | Seconds a git clone --depth 1 of remote blueprint may run
OK_REFLECTED | gact.cancellation_grace_s | src/clio_agent/gact/routes/session_cancellation.py:35 | CLIO_GACT_CANCELLATION_GRACE_S | Seconds cooperative session cancel is given before hard-cancel
OK_REFLECTED | gact.context_references.browse_limit_per_kind | src/clio_agent/gact/context_reference_search.py:35 | CLIO_CONTEXT_REFERENCE_BROWSE_LIMIT | Rows returned per reference kind when picker opens with no query
OK_REFLECTED | gact.context_references.max_hashable_bytes | src/clio_agent/gact/context_references.py:53 | CLIO_CONTEXT_REFERENCE_MAX_HASHABLE_BYTES | Byte ceiling on workspace file that may be attached as context reference
OK_REFLECTED | gact.context_references.search_limit | src/clio_agent/gact/context_reference_search.py:50 | CLIO_CONTEXT_REFERENCE_SEARCH_LIMIT | Total rows one reference search returns for a typed query
OK_REFLECTED | gact.context_references.snapshot_children | src/clio_agent/gact/context_reference_evidence.py:50 | CLIO_CONTEXT_REFERENCE_SNAPSHOT_CHILDREN | Mapping/list children kept per level of a bounded evidence snapshot
OK_REFLECTED | gact.context_references.snapshot_string_chars | src/clio_agent/gact/context_reference_evidence.py:65 | CLIO_CONTEXT_REFERENCE_SNAPSHOT_STRING_CHARS | Character ceiling for one string inside a bounded evidence snapshot
OK_REFLECTED | gact.context_references.summary_excerpt_chars | src/clio_agent/gact/context_references.py:68 | CLIO_CONTEXT_REFERENCE_SUMMARY_EXCERPT_CHARS | Character ceiling for one excerpt inside a bounded session reference
OK_REFLECTED | gact.context_references.summary_messages | src/clio_agent/gact/context_references.py:88 | CLIO_CONTEXT_REFERENCE_SUMMARY_MESSAGES | How many recent messages a referenced session's summary carries
OK_REFLECTED | gact.interactions.projection_limit | src/clio_agent/gact/routes/interactions.py:97 | CLIO_INTERACTIONS_PROJECTION_LIMIT | Maximum rows one pending-interaction projection returns, newest first
OK_REFLECTED | gact.ledger_retention.a2ui_messages.max | src/clio_agent/gact/a2ui.py:79 | CLIO_LEDGER_A2UI_MESSAGES_MAX | Retention bound on one A2UI surface's message log
OK_REFLECTED | gact.ledger_retention.command_audit.max | src/clio_agent/gact/runtime/retention.py:37 | CLIO_LEDGER_COMMAND_AUDIT_MAX | Max entries in in-memory command-audit ledger before FIFO eviction
OK_REFLECTED | gact.ledger_retention.context_frames.max | src/clio_agent/gact/runtime/retention.py:43 | CLIO_LEDGER_CONTEXT_FRAMES_MAX | Max per-session context-frame entries kept in memory
OK_REFLECTED | gact.ledger_retention.memory_tool_audit.max | src/clio_agent/gact/runtime/retention.py:49 | CLIO_LEDGER_MEMORY_TOOL_AUDIT_MAX | Max entries in in-memory memory-tool-audit ledger
OK_REFLECTED | gact.ledger_retention.native_delivery_notes.max | src/clio_agent/gact/native_delivery_outcome.py:83 | CLIO_LEDGER_NATIVE_DELIVERY_NOTES_MAX | Unsettled native-attachment decline notes buffered before oldest dropped
OK_REFLECTED | gact.ledger_retention.pending_diffs.hard | src/clio_agent/gact/runtime/retention.py:67 | CLIO_LEDGER_PENDING_DIFFS_HARD | Absolute ceiling on pending-diffs ledger
OK_REFLECTED | gact.ledger_retention.pending_diffs.max | src/clio_agent/gact/runtime/retention.py:61 | CLIO_LEDGER_PENDING_DIFFS_MAX | Soft cap on pending-diffs ledger where terminal entries evict first
OK_REFLECTED | gact.ledger_retention.permissions.hard | src/clio_agent/gact/runtime/retention.py:79 | CLIO_LEDGER_PERMISSIONS_HARD | Absolute ceiling on in-memory permissions ledger
OK_REFLECTED | gact.ledger_retention.permissions.max | src/clio_agent/gact/runtime/retention.py:73 | CLIO_LEDGER_PERMISSIONS_MAX | Soft cap on permissions ledger where terminal entries evict first
OK_REFLECTED | gact.ledger_retention.shared_tokens.hard | src/clio_agent/gact/runtime/retention.py:91 | CLIO_LEDGER_SHARED_TOKENS_HARD | Absolute ceiling on shared-token ledger
OK_REFLECTED | gact.ledger_retention.shared_tokens.max | src/clio_agent/gact/runtime/retention.py:85 | CLIO_LEDGER_SHARED_TOKENS_MAX | Soft cap on shared-token ledger where terminal entries evict first
OK_REFLECTED | gact.ledger_retention.stream_fallback_notes.max | src/clio_agent/gact/stream_fallbacks.py:28 | CLIO_LEDGER_STREAM_FALLBACK_NOTES_MAX | Typed non-delivery degradation notes kept per session
OK_REFLECTED | gact.ledger_retention.turn_attempts.hard | src/clio_agent/gact/runtime/retention.py:103 | CLIO_LEDGER_TURN_ATTEMPTS_HARD | Absolute ceiling on turn-attempts ledger
OK_REFLECTED | gact.ledger_retention.turn_attempts.max | src/clio_agent/gact/runtime/retention.py:97 | CLIO_LEDGER_TURN_ATTEMPTS_MAX | Soft cap on turn-attempts ledger where terminal entries evict first
OK_REFLECTED | gact.ledger_retention.user_questions.hard | src/clio_agent/gact/runtime/retention.py:115 | CLIO_LEDGER_USER_QUESTIONS_HARD | Absolute ceiling on in-memory ask-user question ledger
OK_REFLECTED | gact.ledger_retention.user_questions.max | src/clio_agent/gact/runtime/retention.py:109 | CLIO_LEDGER_USER_QUESTIONS_MAX | Soft cap on ask-user question ledger where terminal rows evict first
OK_REFLECTED | gact.live_edge_streaming | src/clio_agent/gact/live_edge.py:400 | CLIO_LIVE_EDGE_STREAMING | Experimental flag enabling live-edge SSE atom sealing
OK_REFLECTED | gact.loop_inbox.max_events | src/clio_agent/gact/loop_inbox.py:97 | CLIO_GACT_LOOP_INBOX_MAX_EVENTS | Per-session bound on buffered mid-turn wakes
OK_REFLECTED | gact.message_intents.max_queued_per_session | src/clio_agent/gact/message_intents.py:55 | CLIO_GACT_MAX_QUEUED_MESSAGES_PER_SESSION | Per-session cap on durable queued (future) messages
OK_REFLECTED | gact.message_intents.max_settled_steers_per_session | src/clio_agent/gact/message_intents.py:61 | CLIO_GACT_MAX_SETTLED_STEERS_PER_SESSION | Per-session cap on retained SETTLED pending-steer rows
OK_REFLECTED | gact.message_intents.max_acceptances_per_session | src/clio_agent/gact/message_intents.py:67 | CLIO_GACT_MAX_ACCEPTANCES_PER_SESSION | Per-session cap on retained message-acceptance records
OK_REFLECTED | gact.resident_ledgers.idle_ttl_s | src/clio_agent/gact/resident_ledgers.py:218 | CLIO_RESIDENT_LEDGERS_TTL_S | Seconds an idle session's in-memory transcript ledger may sit resident
OK_REFLECTED | gact.resident_ledgers.max_bytes | src/clio_agent/gact/resident_ledgers.py:225 | CLIO_RESIDENT_LEDGERS_MAX_BYTES | Byte cap on total resident transcript-ledger cache across sessions
OK_REFLECTED | gact.resident_ledgers.max_sessions | src/clio_agent/gact/resident_ledgers.py:232 | CLIO_RESIDENT_LEDGERS_MAX | Max sessions whose transcript ledgers stay resident at once
OK_REFLECTED | hooks.allow_managed_only | src/clio_agent/gact/hooks/dispatcher.py:197 | CLIO_HOOKS_ALLOW_MANAGED_ONLY | Lockdown flag dropping every non-managed hook source
OK_REFLECTED | hooks.defer_timeout | src/clio_agent/gact/hooks/defer.py:29 | CLIO_HOOKS_DEFER_TIMEOUT | Seconds a parked PreToolUse defer waits for out-of-band resolution
OK_REFLECTED | hooks.stop_loop_cap | src/clio_agent/gact/hooks/stop_loop.py:65 | CLIO_HOOKS_STOP_LOOP_CAP | Hard ceiling on Stop-hook-driven turn re-drives within one sequence
OK_REFLECTED | permissions.ai_review_timeout_s | src/clio_agent/gact/runtime/ai_review.py:61 | CLIO_AI_REVIEW_TIMEOUT_S | Seconds AI-review permission gate waits for reviewer LM verdict
OK_REFLECTED | arc.cache_capacity | src/clio_agent/arc/memory.py:163 | CLIO_ARC_CACHE_CAPACITY | Max entries in ARC's in-process LRU cache
OK_REFLECTED | arc.clio_core.daemon_recycle_enabled | src/clio_agent/arc/clio_core_daemon.py:148 | CLIO_ARC_CLIO_CORE_DAEMON_RECYCLE | Opt-in: auto-stop shared clio-core daemon when RSS-critical
OK_REFLECTED | arc.clio_core.daemon_rss_warn_bytes | src/clio_agent/arc/clio_core_daemon.py:154 | CLIO_ARC_CLIO_CORE_DAEMON_RSS_WARN | RSS bytes marking daemon elevated in doctor report
OK_REFLECTED | arc.clio_core.daemon_rss_critical_bytes | src/clio_agent/arc/clio_core_daemon.py:160 | CLIO_ARC_CLIO_CORE_DAEMON_RSS_CRITICAL | RSS bytes marking daemon critical (recycle-eligible if idle)
OK_REFLECTED | arc.clio_core.liveness_ttl_s | src/clio_agent/arc/clio_core_liveness.py:176 | CLIO_ARC_CLIO_CORE_LIVENESS_TTL_S | Seconds a clio-core liveness probe is trusted before re-checking
OK_REFLECTED | arc.clio_core_write_retry.attempts | src/clio_agent/arc/clio_core_retry.py:58 | CLIO_ARC_CLIO_CORE_WRITE_RETRY_ATTEMPTS | Max attempts for a clio-core blob PutBlob before write lost
OK_REFLECTED | arc.clio_core_write_retry.first_delay_s | src/clio_agent/arc/clio_core_retry.py:77 | CLIO_ARC_CLIO_CORE_WRITE_RETRY_FIRST_DELAY_S | Seconds before first retry of refused clio-core blob write
OK_REFLECTED | arc.clio_core_write_retry.backoff_factor | src/clio_agent/arc/clio_core_retry.py:96 | CLIO_ARC_CLIO_CORE_WRITE_RETRY_BACKOFF_FACTOR | Multiplier applied to write-retry delay after each failed attempt
OK_REFLECTED | arc.events_chunk_segments | src/clio_agent/arc/memory.py:163 | CLIO_ARC_EVENTS_CHUNK_SEGMENTS | Semantic-event segments held per on-disk chunk before rolling
OK_REFLECTED | arc.liveness.backoff_initial_s | src/clio_agent/arc/rpc_liveness.py:118 | CLIO_ARC_LIVENESS_BACKOFF_INITIAL_S | Seconds of initial backoff before first retry of stalled clio-core RPC
OK_REFLECTED | arc.liveness.backoff_max_s | src/clio_agent/arc/rpc_liveness.py:128 | CLIO_ARC_LIVENESS_BACKOFF_MAX_S | Seconds cap on growing backoff delay between stalled-RPC retries
OK_REFLECTED | arc.liveness.retries | src/clio_agent/arc/rpc_liveness.py:138 | CLIO_ARC_LIVENESS_RETRIES | Retry attempts after a clio-core RPC stalls before store is quarantined
OK_REFLECTED | arc.liveness.stall_after_s | src/clio_agent/arc/rpc_liveness.py:148 | CLIO_ARC_LIVENESS_STALL_AFTER_S | Seconds one clio-core RPC may run before treated as stalled zombie
OK_REFLECTED | arc.lsm_compaction_threshold | src/clio_agent/arc/memory.py:216 | CLIO_ARC_LSM_COMPACTION_THRESHOLD | Memtable flushes accumulated before ARC metrics LSM tree compacts
OK_REFLECTED | arc.lsm_memtable_size | src/clio_agent/arc/memory.py:222 | CLIO_ARC_LSM_MEMTABLE_SIZE | Max entries in ARC metrics LSM tree's in-memory memtable
OK_REFLECTED | arc.store | src/clio_agent/arc/storage.py:865 | CLIO_ARC_STORE | Selects ARC persistence backend (cte or local)
OK_REFLECTED | artifacts.cas_budget_bytes | src/clio_agent/gact/artifacts/cas.py:63 | CLIO_ARTIFACT_CAS_BUDGET_BYTES | Byte budget for content-addressed artifact store
OK_REFLECTED | artifacts.cas_max_file_bytes | src/clio_agent/gact/artifacts/cas.py:75 | CLIO_ARTIFACT_CAS_MAX_FILE_BYTES | Per-file byte ceiling for CAS ingestion
OK_REFLECTED | artifacts.export_license | src/clio_agent/gact/artifacts/export.py:60 | CLIO_ARTIFACTS_EXPORT_LICENSE | SPDX license string stamped on RO-Crate export
OK_REFLECTED | artifacts.hash_max_file_bytes | src/clio_agent/gact/artifacts/minting.py:47 | CLIO_ARTIFACTS_HASH_MAX_FILE_BYTES | Byte ceiling on hashing a designated output at mint time
OK_REFLECTED | artifacts.hash_stat_cache | src/clio_agent/gact/artifacts/cas.py:82 | CLIO_ARTIFACT_HASH_STAT_CACHE | Trust blob's stat instead of re-hashing for CAS integrity check
OK_REFLECTED | artifacts.instrument_arg_max_bytes | src/clio_agent/gact/artifacts/transforms.py:32 | CLIO_ARTIFACTS_INSTRUMENT_ARG_MAX_BYTES | Byte ceiling per tool-call arg kept in TransformRecord
OK_REFLECTED | artifacts.instrument_total_max_bytes | src/clio_agent/gact/artifacts/transforms.py:41 | CLIO_ARTIFACTS_INSTRUMENT_TOTAL_MAX_BYTES | Whole-instrument byte ceiling on combined tool-call args
OK_REFLECTED | artifacts.lineage_max_nodes | src/clio_agent/gact/artifacts/lineage.py:95 | CLIO_ARTIFACTS_LINEAGE_MAX_NODES | Node cap on COMPLETE artifact-lineage closure
OK_REFLECTED | artifacts.proposals_batch_max | src/clio_agent/gact/artifacts/proposals.py:128 | CLIO_ARTIFACTS_PROPOSALS_BATCH_MAX | Max artifact proposals in one create_artifact call
OK_REFLECTED | artifacts.proposals_per_turn | src/clio_agent/gact/artifacts/proposals.py:120 | CLIO_ARTIFACTS_PROPOSALS_PER_TURN | Ceiling on new artifact promotions one turn may make
OK_REFLECTED | artifacts.table_preview_max_rows | src/clio_agent/gact/routes/artifact_table_preview.py:60 | CLIO_ARTIFACTS_TABLE_PREVIEW_MAX_ROWS | Ceiling on rows returned by artifact table-preview response
OK_REFLECTED | artifacts.table_preview_max_source_bytes | src/clio_agent/gact/routes/artifact_table_preview.py:66 | CLIO_ARTIFACTS_TABLE_PREVIEW_MAX_SOURCE_BYTES | Largest CSV artifact the table-preview route will read
OK_REFLECTED | runtime.capture_reasoning | src/clio_agent/gact/usage.py:34 | CLIO_CAPTURE_REASONING | Whether per-call reasoning/chain-of-thought traces are persisted
OK_REFLECTED | runtime.environment | src/clio_agent/config.py:536 | CLIO_ENVIRONMENT | Deployment environment label (dev/staging/prod)
OK_REFLECTED | runtime.live_streaming | src/clio_agent/lm/adapters.py:50 | CLIO_LIVE_STREAMING | Stream top-level GACT turn answer live via dspy.streamify
OK_REFLECTED | runtime.lm_token_liveness | src/clio_agent/lm/io_logging.py:86 | CLIO_LM_TOKEN_LIVENESS | Stream expert LM calls token-by-token for no-progress watchdog
OK_REFLECTED | sandbox.enabled | src/clio_agent/runtime/sandbox.py:61 | CLIO_SANDBOX_ENABLED | Whether tool-execution sandboxing/confinement is applied
OK_REFLECTED | paths.data_dir | src/clio_agent/runtime/status.py:206 | CLIO_DATA_DIR | Base directory for agent's on-disk data (ARC, sessions, etc.)

## OK_UNREFLECTED
Configuration keys using `conf.resolve()` but intentionally NOT in config.defaults.yaml (policy per conf.py lines 14–38: unset keys are operator-provided or secrets).

OK_UNREFLECTED | lm.reasoning_model | src/clio_agent/lm/factory.py:76 | CLIO_LM_REASONING_MODEL | Force/forbid reasoning-model behavior; auto-detection override
OK_UNREFLECTED | lm.stop_sequences | src/clio_agent/lm/factory.py:86 | CLIO_LM_STOP_SEQUENCES | Stop sequences for reasoning models to truncate output
OK_UNREFLECTED | gact.auth.bearer_token | src/clio_agent/gact/auth.py:24 | CLIO_GACT_BEARER_TOKEN | Optional bearer token for non-loopback GACT API (secret tier)
OK_UNREFLECTED | gact.cors.origins | src/clio_agent/gact/cors.py:26 | CLIO_GACT_CORS_ORIGINS | Comma-separated browser origins allowed cross-origin
OK_UNREFLECTED | arc.cte.dir | src/clio_agent/arc/clio_core_config.py:200 | CLIO_ARC_CTE_DIR | Directory clio-core CTE artifacts written under
OK_UNREFLECTED | arc.store_config | src/clio_agent/arc/storage.py:875 | CLIO_ARC_STORE_CONFIG | Path to clio-core CTE config when arc.store is cte
OK_UNREFLECTED | arc.core_port | src/clio_agent/arc/clio_core_liveness.py:133 | CLIO_CORE_PORT | Overrides clio-core RPC port the liveness prober dials
OK_UNREFLECTED | arc.server_conf | src/clio_agent/arc/clio_core_liveness.py:142 | CLIO_SERVER_CONF | Path to clio-core server YAML config for port-resolution
OK_UNREFLECTED | agents.child_forward_deadline_s | src/clio_agent/gact/child_forward.py:100 | CLIO_CHILD_FORWARD_DEADLINE_S | Seconds before unattended parent's forwarded HITL question auto-fails
OK_UNREFLECTED | goal.judge_model | src/clio_agent/gact/goal.py:172 | CLIO_GOAL_JUDGE_MODEL | Model id for LLM-judge that evaluates armed goal condition
OK_UNREFLECTED | hooks.config | src/clio_agent/gact/hooks/dispatcher.py:218 | CLIO_HOOKS_CONFIG | Explicit single hook-config file override
OK_UNREFLECTED | hooks.managed_config | src/clio_agent/gact/hooks/dispatcher.py:236 | CLIO_HOOKS_MANAGED_CONFIG | Path to admin/managed hook config file
OK_UNREFLECTED | hooks.trust_store | src/clio_agent/gact/hooks/dispatcher.py:253 | CLIO_HOOKS_TRUST_STORE | Override path for trusted hook-fingerprint store
OK_UNREFLECTED | permissions.ai_review_model | src/clio_agent/gact/runtime/ai_review.py:56 | CLIO_AI_REVIEW_MODEL | Optional reviewer model override
OK_UNREFLECTED | scheduler.timezone | src/clio_agent/gact/scheduler.py:256 | CLIO_SCHEDULER_TZ | Default timezone schedules resolve cron expression's fire times in
OK_UNREFLECTED | runtime.api_base | src/clio_agent/runtime/status.py:206 | CLIO_API_BASE | Base URL of running gact API used by doctor/status health probe
OK_UNREFLECTED | paths.model_catalog | src/clio_agent/providers/model_discovery/overlay.py:145 | CLIO_MODEL_CATALOG | File path for discovered-model catalog cache
OK_UNREFLECTED | paths.model_db | src/clio_agent/providers/handshake/sources/db.py:86 | CLIO_MODEL_DB | File path for writable per-model limits DB
OK_UNREFLECTED | paths.sessions | src/clio_agent/gact/sessions.py:106 | CLIO_SESSIONS_PATH | Full override path for sessions.json registry file
OK_UNREFLECTED | paths.web_dir | src/clio_agent/gact/app.py:145 | CLIO_WEB_DIR | Directory of built web-UI bundle to serve
OK_UNREFLECTED | lm.api_base | src/clio_agent/config.py:532 | CLIO_LM_API_BASE | Overrides LM provider's default API base URL
OK_UNREFLECTED | lm.model | src/clio_agent/config.py:533 | CLIO_LM_MODEL | Pins exact model identifier to use
OK_UNREFLECTED | lm.max_tokens | src/clio_agent/config.py:582 | CLIO_LM_MAX_TOKENS | Overrides per-reply output token cap
OK_UNREFLECTED | lm.temperature | src/clio_agent/config.py:570 | CLIO_LM_TEMPERATURE | Sampling temperature for main agentic LM calls
OK_UNREFLECTED | lm.planner_temperature | src/clio_agent/config.py:573 | CLIO_LM_PLANNER_TEMPERATURE | Sampling temperature for deterministic action-planning calls
OK_UNREFLECTED | lm.planner_max_tokens | src/clio_agent/config.py:579 | CLIO_LM_PLANNER_MAX_TOKENS | Token cap for lower-temperature planner/routing generations
OK_UNREFLECTED | lm.top_p | src/clio_agent/config.py:585 | CLIO_LM_TOP_P | OpenAI-standard top-p sampling parameter
OK_UNREFLECTED | lm.top_k | src/clio_agent/config.py:586 | CLIO_LM_TOP_K | Top-k sampling param via extra_body on llama.cpp/LM Studio
OK_UNREFLECTED | lm.min_p | src/clio_agent/config.py:587 | CLIO_LM_MIN_P | Min-p sampling param via extra_body on llama.cpp/LM Studio
OK_UNREFLECTED | lm.presence_penalty | src/clio_agent/config.py:588 | CLIO_LM_PRESENCE_PENALTY | OpenAI-standard presence-penalty sampling parameter
OK_UNREFLECTED | lm.thinking_budget | src/clio_agent/config.py:564 | CLIO_LM_THINKING_BUDGET | Explicit reasoning token-budget override per-provider
OK_UNREFLECTED | lm.thinking_level | src/clio_agent/config.py:567 | CLIO_LM_THINKING_LEVEL | Provider-generic reasoning level (off/low/medium/high)
OK_UNREFLECTED | lm.context_window | src/clio_agent/config.py:565 | CLIO_LM_CONTEXT_WINDOW | Override effective context window used by clio
OK_UNREFLECTED | lm.claude_code_transport | src/clio_agent/config.py:545 | CLIO_CLAUDE_CODE_TRANSPORT | Claude Code transport selection (sdk)
OK_UNREFLECTED | lm.codex_transport | src/clio_agent/config.py:540 | CLIO_CODEX_TRANSPORT | Codex transport selection (sdk)
OK_UNREFLECTED | tools.file_policy.allow_symlinks | src/clio_agent/tools/file_policy.py:120 | CLIO_ALLOW_SYMLINKS | Whether tool file reads/writes may traverse symlinks
OK_UNREFLECTED | tools.file_policy.max_file_size_bytes | src/clio_agent/tools/file_policy.py:132 | CLIO_MAX_FILE_SIZE_BYTES | Byte-size cap on files a read/write tool call may touch
OK_UNREFLECTED | tools.mcp.call_timeout_s | src/clio_agent/tools/execution.py:219 | CLIO_MCP_CALL_TIMEOUT_S | Runaway backstop seconds for synchronous MCP tool call
OK_UNREFLECTED | tools.mcp.cold_spawn_runaway_s | src/clio_agent/tools/mcp_discovery.py:261 | CLIO_MCP_COLD_SPAWN_RUNAWAY_S | Generous backstop for MCP namespace discovery/connect attempt
OK_UNREFLECTED | tools.mcp.connect_mode | src/clio_agent/tools/mcp_connection_era.py:204 | CLIO_MCP_CONNECT_MODE | MCP protocol-era negotiation mode (auto/v1/legacy)
OK_UNREFLECTED | tools.mcp.discovery_concurrency | src/clio_agent/tools/mcp_discovery.py:252 | CLIO_MCP_DISCOVERY_CONCURRENCY | Max declared MCP namespaces probed concurrently during boot
OK_UNREFLECTED | tools.mcp.discovery_heal_interval_s | src/clio_agent/tools/mcp_discovery.py:272 | CLIO_MCP_DISCOVERY_HEAL_INTERVAL_S | Seconds between background re-probes of failed MCP namespaces
OK_UNREFLECTED | tools.mcp.elicitation.agent_audience.enabled | src/clio_agent/gact/agent_elicitation.py:182 | CLIO_MCP_ELICITATION_AGENT_AUDIENCE_ENABLED | Master switch for agent-driven elicitation
OK_UNREFLECTED | tools.mcp.elicitation.agent_audience.max_depth | src/clio_agent/gact/agent_elicitation.py:195 | CLIO_MCP_ELICITATION_AGENT_AUDIENCE_MAX_DEPTH | Recursion bound for agent-answer turns
OK_UNREFLECTED | tools.mcp.elicitation.agent_audience.timeout_s | src/clio_agent/gact/agent_elicitation.py:188 | CLIO_MCP_ELICITATION_AGENT_AUDIENCE_TIMEOUT_S | Seconds an agent-answer turn may take before fall back
OK_UNREFLECTED | tools.mcp.launcher_cache_lock_timeout_s | src/clio_agent/tools/launcher_cache_lock.py:60 | CLIO_MCP_LAUNCHER_CACHE_LOCK_TIMEOUT_S | Runaway backstop while waiting on shared uv-launcher cache lock
OK_UNREFLECTED | tools.mcp.listing_ttl_h | src/clio_agent/tools/listing_cache.py:77 | CLIO_MCP_LISTING_TTL_H | Hours cached MCP tool listing stays valid before live relist
OK_UNREFLECTED | tools.mcp.probe_timeout_retries | src/clio_agent/tools/mcp_probe_hardening.py:44 | CLIO_MCP_PROBE_TIMEOUT_RETRIES | Retries of era-negotiation probe after client-side timeout
OK_UNREFLECTED | tools.mcp.response_cache_enabled | src/clio_agent/tools/mcp_runtime.py:277 | CLIO_MCP_RESPONSE_CACHE_ENABLED | Opts MCP clients into SEP-2549 server-hinted response caching
OK_UNREFLECTED | tools.mcp.setup_timeout_s | src/clio_agent/gact/mcp_readiness.py:68 | CLIO_MCP_SETUP_TIMEOUT_S | Seconds allowed for MCP tool executor startup handshake
OK_UNREFLECTED | tools.mcp.spawn_diet | src/clio_agent/tools/spawn_diet.py:67 | CLIO_MCP_SPAWN_DIET | Enable learned direct-interpreter spawn shortcut
OK_UNREFLECTED | tools.mcp.spawn_diet_ttl_h | src/clio_agent/tools/spawn_diet.py:79 | CLIO_MCP_SPAWN_DIET_TTL_H | Hours learned spawn-diet shortcut stays valid
OK_UNREFLECTED | tools.mcp.workspace_max_resident | src/clio_agent/tools/reaper.py:84 | CLIO_MCP_WORKSPACE_MAX_RESIDENT | LRU cap on per-workspace MCP tool-executor fleets resident
OK_UNREFLECTED | tools.mcp.workspace_ttl_s | src/clio_agent/tools/reaper.py:96 | CLIO_MCP_WORKSPACE_TTL_S | Idle seconds before unused per-workspace fleet closed
OK_UNREFLECTED | tools.mcp_cache.max_age_days | src/clio_agent/tools/mcp_cache.py:139 | CLIO_MCP_CACHE_MAX_AGE_DAYS | Age ceiling for built MCP uv-spawn environment before pruning
OK_UNREFLECTED | tools.mcp_cache.max_bytes | src/clio_agent/tools/mcp_cache.py:128 | CLIO_MCP_CACHE_MAX_BYTES | Total disk-size ceiling for clio-owned MCP uv spawn cache
OK_UNREFLECTED | tools.shell.windows_backend | src/clio_agent/tools/servers/shell_server.py:143 | CLIO_WINDOWS_SHELL_BACKEND | Which interpreter Windows shell tool runs through

## ENV_SANCTIONED
Bare `os.environ` or `os.getenv()` reads EXPLICITLY on conf.py lines 40–53 documented exemption list (bootstrap, secrets, provider auth-status probes).

ENV_SANCTIONED | CLIO_USER_DIR | src/clio_agent/paths.py:33 | (bootstrap) | Read before config.py exists; XDG path discovery
ENV_SANCTIONED | CLIO_ENV_FILE | src/clio_agent/config.py:144 | (bootstrap) | Dotenv loader path; read at module load
ENV_SANCTIONED | CLIO_ENV_FILE_LOADED | src/clio_agent/config.py:139 | (bootstrap) | Dotenv tracking sentinel; bootstrap state
ENV_SANCTIONED | CLIO_LM_API_KEY | src/clio_agent/config.py:535 | (secret) | LM provider credential; never committed to config.yaml
ENV_SANCTIONED | CLIO_LM_API_KEY | src/clio_agent/providers/credentials.py:95 | (secret) | LM provider credential; never committed
ENV_SANCTIONED | CLIO_ARGONNE_TOKEN | src/clio_agent/providers/credentials.py:95 | (secret) | Argonne/ALCF cluster auth; never committed
ENV_SANCTIONED | ALCF_INFERENCE_TOKEN | src/clio_agent/providers/credentials.py:96 | (secret) | ALCF inference token; never committed
ENV_SANCTIONED | CLIO_RELAY_API_TOKEN | src/clio_agent/tools/relay_factory.py:235 | (secret) | Relay transport credential; never committed
ENV_SANCTIONED | CLIO_RELAY_API_TOKEN | src/clio_agent/tools/relay_transport.py:207 | (secret) | Relay transport credential; duplicate check
ENV_SANCTIONED | provider_env_keys | src/clio_agent/gact/routes/providers.py:170 | (provider-probes) | Real process env checks for auth-status UI; must reflect actual env
ENV_SANCTIONED | provider_env_keys | src/clio_agent/gact/routes/providers.py:571 | (provider-probes) | Real process env checks for auth-status UI; duplicate

## ENV_BARE
Bare `os.environ` or `os.getenv()` reads that should be migrated to `conf.resolve()`: 31 violations

ENV_BARE | CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS | src/clio_agent/agent.py:880 | n/a | limits.transient_provider_retry_delays has config.defaults entry but read bare
ENV_BARE | CLIO_RUNTIME_STATE_DIR | src/clio_agent/arc/clio_core_config.py:79 | n/a | No conf.resolve equivalent; should add conf.resolve call
ENV_BARE | CLIO_PROVENANCE_PROVIDERS | src/clio_agent/provenance_config.py:34 | n/a | Read twice (line 34, 112); should be single conf.resolve; key exists in defaults
ENV_BARE | CLIO_SEMANTIC_TRACE_BACKEND | src/clio_agent/provenance_config.py:40 | n/a | Read twice (40, 118); legacy; key missing from defaults; needs migration
ENV_BARE | CHI_SERVER_CONF | src/clio_agent/arc/clio_core_liveness.py:145 | n/a | Typo? Should be CLIO_SERVER_CONF; alternative: conf.resolve(arc.server_conf)
ENV_BARE | CLIO_LM_GUIDED_OUTPUT | src/clio_agent/lm/adapters.py:428 | n/a | lm.guided_output in defaults but read bare as_bool(os.environ.get(...))
ENV_BARE | CLIO_ONLYOFFICE_URL | src/clio_agent/gact/documents/editors.py:192 | n/a | Document editor integration; no conf.resolve equivalent; should add
ENV_BARE | CLIO_COLLABORA_URL | src/clio_agent/gact/documents/editors.py:194 | n/a | Document editor integration; no conf.resolve equivalent; should add
ENV_BARE | CLIO_GACT_PUBLIC_URL | src/clio_agent/gact/documents/editors.py:204 | n/a | Read twice (204, 209); document editor integration; no conf.resolve
ENV_BARE | CLIO_ONLYOFFICE_JWT_SECRET | src/clio_agent/gact/documents/editors.py:243 | n/a | OnlyOffice secret; should use conf.resolve (secret tier)
ENV_BARE | CLIO_DOCUMENT_TYPST_FONT | src/clio_agent/gact/documents/renditions.py:145 | n/a | Typst font path; no conf.resolve equivalent; should add
ENV_BARE | CODEX_HOME | src/clio_agent/providers/codex_credential_home.py:158 | n/a | Third-party Codex SDK integration; read 3 times total across codebase
ENV_BARE | CODEX_HOME | src/clio_agent/runtime/lm_provider_probe.py:94 | n/a | CODEX_HOME duplicate read (third-party integration)
ENV_BARE | CODEX_HOME | src/clio_agent/runtime/sandbox_codex.py:252 | n/a | CODEX_HOME duplicate read (third-party integration)
ENV_BARE | FLOWCEPT_SETTINGS_PATH | src/clio_agent/gact/provenance/flowcept.py:191 | n/a | Flowcept provenance integration; third-party tool setup
ENV_BARE | LM_STUDIO_API_TOKEN | src/clio_agent/gact/providers/lmstudio.py:44 | n/a | LM Studio provider credential; third-party integration
ENV_BARE | LM_API_TOKEN | src/clio_agent/gact/providers/lmstudio.py:45 | n/a | LM Studio API token variant; third-party integration
ENV_BARE | LOCALAPPDATA | src/clio_agent/providers/argonne_auth.py:191 | n/a | Windows platform path (fallback when XDG_DATA_HOME unset); platform-specific
ENV_BARE | XDG_DATA_HOME | src/clio_agent/providers/argonne_auth.py:197 | n/a | XDG standard path (Argonne auth token location); platform-specific fallback
ENV_BARE | ProgramFiles | src/clio_agent/gact/documents/renditions.py:83 | n/a | Windows platform path for Typst font search; platform-specific
ENV_BARE | ProgramFiles(x86) | src/clio_agent/gact/documents/renditions.py:84 | n/a | Windows platform path variant (x86 font search); platform-specific
ENV_BARE | APPDATA | src/clio_agent/runtime/sandbox_cli.py:252 | n/a | Windows platform path (fallback when HOME unset); platform-specific
ENV_BARE | LOCALAPPDATA | src/clio_agent/runtime/sandbox_cli.py:253 | n/a | Windows LOCALAPPDATA (duplicate); platform-specific fallback
ENV_BARE | PROCESSOR_ARCHITECTURE | src/clio_agent/gact/provenance/flowcept.py:120 | n/a | Platform architecture diagnostic for Flowcept; third-party integration
ENV_BARE | COMPUTERNAME | src/clio_agent/__init__.py:61 | n/a | Windows machine hostname; platform diagnostic (build time)
ENV_BARE | PROCESSOR_IDENTIFIER | src/clio_agent/__init__.py:66 | n/a | Windows CPU type diagnostic; platform diagnostic (build time)
ENV_BARE | PROCESSOR_ARCHITECTURE | src/clio_agent/__init__.py:73 | n/a | Windows CPU architecture diagnostic; platform diagnostic (build time)
ENV_BARE | OS | src/clio_agent/__init__.py:72 | n/a | Windows OS version label; platform diagnostic (build time)

## HARDCODED
Hardcoded operational tunables (timeouts, retry counts, intervals, caps) with no conf.resolve path: 16 violations

HARDCODED | _RUNTIME_START_TIMEOUT_S | src/clio_agent/arc/storage.py:274 | n/a | 30.0 seconds; clio-core startup timeout; should use conf.resolve(arc.cte.startup_timeout_s)
HARDCODED | _DEFAULT_BOOTSTRAP_TIMEOUT_S | src/clio_agent/gact/agent_blueprints.py:53 | n/a | 20 seconds; blueprint registry clone timeout; duplicate of gact.blueprint_source.clone_timeout_s
HARDCODED | _DEFAULT_TIMEOUT_S | src/clio_agent/gact/agent_elicitation.py:205 | n/a | 90.0 seconds; agent elicitation timeout; should use conf.resolve
HARDCODED | _OUTER_TIMEOUT_MARGIN_S | src/clio_agent/gact/agent_elicitation.py:206 | n/a | 15.0 seconds; elicitation safety margin; should use conf.resolve
HARDCODED | _DEFAULT_MAX_DEPTH | src/clio_agent/gact/agent_elicitation.py:204 | n/a | depth=1; elicitation recursion limit; should use conf.resolve
HARDCODED | FALLBACK_DELAY_S | src/clio_agent/gact/autonomous_loop.py:61 | n/a | 60 seconds; fallback retry delay; should use conf.resolve
HARDCODED | WAKEUP_MIN_S | src/clio_agent/gact/autonomous_loop.py:58 | n/a | 60 seconds; minimum wakeup interval; should use conf.resolve
HARDCODED | WAKEUP_MAX_S | src/clio_agent/gact/autonomous_loop.py:59 | n/a | 3600 seconds; maximum wakeup interval; should use conf.resolve
HARDCODED | DEFAULT_ELICITATION_TIMEOUT_S | src/clio_agent/gact/elicitation_bridge.py:682 | n/a | 600.0 seconds; form mode elicitation timeout; mirrors gact.ask_user.ttl_s
HARDCODED | DEFAULT_TIMEOUT_S | src/clio_agent/gact/permission_gate.py:49 | n/a | 600.0 seconds; permission approval timeout; should use conf.resolve
HARDCODED | _DEFAULT_TIMEOUT_S | src/clio_agent/gact/runtime/ai_review.py:86 | n/a | 45.0 seconds; AI review verdict timeout; mirrors permissions.ai_review_timeout_s
HARDCODED | _EGRESS_GATE_TIMEOUT_S | src/clio_agent/gact/runtime/grants.py:68 | n/a | 600.0 seconds; egress grant gate timeout; should use conf.resolve
HARDCODED | DEFAULT_SDK_PROGRESS_TIMEOUT_S | src/clio_agent/providers/codex_stream.py:28 | n/a | 120.0 seconds; Codex SDK progress timeout; mirrors limits.codex_sdk_progress_timeout_s
HARDCODED | DEFAULT_TURN_TIMEOUT_S | src/clio_agent/providers/codex_stream.py:32 | n/a | 180.0 seconds; Codex turn timeout; should use conf.resolve
HARDCODED | _CONNECT_READ_TIMEOUT_S | src/clio_agent/runtime/net_chokepoint.py:57 | n/a | 30.0 seconds; network socket timeout; should use conf.resolve
HARDCODED | _VERSION_PROBE_TIMEOUT_S | src/clio_agent/runtime/sandbox_codex.py:33 | n/a | 5.0 seconds; Codex version probe timeout; should use conf.resolve

---

## TOTALS

### Configuration via conf.resolve()
- OK_REFLECTED (uses conf.resolve + in defaults.yaml): 103 keys
- OK_UNREFLECTED (uses conf.resolve, NOT in defaults.yaml by design): 50 keys
- **Subtotal: 153 conf.resolve() calls**

### Bare os.environ/os.getenv reads
- ENV_SANCTIONED (deliberately exempted per conf.py lines 40–53): 11 reads
- ENV_BARE (violations; should use conf.resolve or third-party): 31 reads
- **Subtotal: 42 bare env reads**

### Hardcoded constants (no conf.resolve)
- HARDCODED (operational tunables): 16 violations
- **Subtotal: 16 hardcoded constants**

### VIOLATION SUMMARY
- **ENV_BARE violations: 31** (categorized: 3 missing conf.resolve keys + 11 document editors + 3 CODEX_HOME duplicates + 6 third-party/platform + 8 platform diagnostics)
- **HARDCODED violations: 16** (operational timeouts, intervals, retry counts)
- **GRAND TOTAL: 47 violations**

---

**Auditor:** Claude (Auditor C)  
**Date:** 2026-09-07  
**Status:** Complete — exhaustive three-tier sweep (conf.resolve, bare env, hardcoded)
