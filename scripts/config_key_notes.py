# Curated per-key notes + section assignment for config.defaults.yaml.
#
# Generator input for scripts/gen_env_reference.py's render_defaults_yaml.
# SECTIONS groups dotted config keys by longest-matching first-component
# prefix; KEY_NOTES carries one operator-facing sentence per key: what it
# controls, its unit, and when to change it.

SECTIONS: list[tuple[str, tuple[str, ...], str]] = [
    (
        "Runtime + paths",
        ("runtime", "paths", "sandbox"),
        "Process identity, filesystem locations and the global runtime toggles.",
    ),
    (
        "Language models + providers",
        ("lm", "providers"),
        "LM sampling/provider selection and the per-provider transport knobs (Claude Code, Codex).",
    ),
    (
        "Limits, timeouts and retries",
        ("limits",),
        "Per-call byte/character/time bounds and bounded-retry budgets across the tool and LM "
        "paths.",
    ),
    (
        "Agents, workflows and scheduling",
        ("agents", "agent_tasks", "workflows", "goal", "scheduler"),
        "Child-agent concurrency, declared-workflow step liveness, goal judging and the cron "
        "scheduler.",
    ),
    (
        "GACT server",
        ("gact", "a2ui", "autocompact", "permissions", "hooks"),
        "The GACT HTTP/SSE server: ledgers, auth, hooks, A2UI payload bounds and context "
        "auto-compaction.",
    ),
    (
        "ARC memory",
        ("arc",),
        "The ARC memory layer: clio-core CTE storage, liveness/retry policy and the in-process "
        "cache.",
    ),
    (
        "Artifacts",
        ("artifacts",),
        "Artifact minting, the content-addressed store, lineage, exports and artifact-provenance "
        "backends.",
    ),
    (
        "Workspace resources",
        ("resources",),
        "The immutable workspace resource service: the upload byte ceiling and the optional "
        "document-processor used to derive structured views.",
    ),
    (
        "Tools + MCP",
        ("tools",),
        "File-policy sandboxing, shell execution bounds and the MCP tool fleet's "
        "discovery/cache/timeouts.",
    ),
    (
        "Relay",
        ("relay",),
        "The clio-relay integration: console/artifact transfer, the install-surface CLI and remote "
        "agents.",
    ),
    (
        "Provenance, trace and debug",
        ("provenance", "trace", "debug"),
        "Agentic provenance export (Flowcept/JSONL), the semantic trace backend and "
        "debug/diagnostic knobs.",
    ),
    (
        "SPOTTER surveillance",
        ("spotter",),
        "The spotter-ai standing watcher and the clearance barrier a protected session's tool "
        "calls wait on.",
    ),
]

KEY_NOTES: dict[str, str] = {
    "a2ui.max_message_bytes": (
        "Byte ceiling on one encoded A2UI server-to-client message; raise only for trusted "
        "producers emitting larger surface updates."
    ),
    "a2ui.max_string_chars": (
        "Character ceiling on any single string inside an A2UI payload; raise only for trusted "
        "producers with legitimately long text fields."
    ),
    "agent_tasks.max_concurrent": (
        "Max child agent-task threads run concurrently at one spawn depth; raise to widen fan-out "
        "on a big host, lower to cap CPU use."
    ),
    "agents.child_forward_deadline_s": (
        "Seconds before an unattended parent's forwarded HITL question auto-fails the waiting "
        "child task; defaults to the elicitation window."
    ),
    "agents.default_blueprint_id": (
        "Installed marketplace Agent Blueprint a fresh deployment bootstraps as its default; set "
        "it to ship a different agent, never to define one in code."
    ),
    "agents.disable_default_registry_bootstrap": (
        "Disables auto-installing the default marketplace Agent-Blueprint registry on first boot; "
        "set true for an offline deployment."
    ),
    "arc.cache_capacity": (
        "Max entries in ARC's in-process LRU cache; raise to cut store re-reads on a low-RAM host, "
        "lower to shrink resident memory."
    ),
    "arc.clio_core.daemon_recycle_enabled": (
        "Opt-in: auto-stops the shared clio-core daemon when RSS-critical with zero live clients; "
        "enable on long-lived hosts to reclaim leaked memory."
    ),
    "arc.clio_core.daemon_rss_critical_bytes": (
        "RSS bytes marking the shared clio-core daemon critical (recycle-eligible if idle); raise "
        "on a box with more RAM headroom."
    ),
    "arc.clio_core.daemon_rss_warn_bytes": (
        "RSS bytes marking the shared clio-core daemon elevated in the doctor report; tune to "
        "match normal daemon growth on this host."
    ),
    "arc.clio_core.liveness_ttl_s": (
        "Seconds a clio-core liveness probe is trusted before re-checking the socket; raise to cut "
        "overhead, lower to catch a dead daemon faster."
    ),
    "arc.clio_core_write_retry.attempts": (
        "Max attempts (incl. the first) for a clio-core blob PutBlob before the write is recorded "
        "lost; raise if refusals under load need more headroom."
    ),
    "arc.clio_core_write_retry.backoff_factor": (
        "Multiplier applied to the write-retry delay after each failed attempt; raise to back off "
        "more aggressively, lower for tighter spacing."
    ),
    "arc.clio_core_write_retry.first_delay_s": (
        "Seconds before the first retry of a refused clio-core blob write; raise to give a "
        "transient pressure/restart condition more time to clear."
    ),
    "arc.core_port": (
        "Overrides the clio-core RPC port the liveness prober and client dial; set when clio-core "
        "is bound to a non-default port."
    ),
    "arc.cte.dir": (
        "Directory clio-core's CTE artifacts (config, file-tier data, logs) are written under; "
        "change to relocate ARC's disk storage."
    ),
    "arc.cte.disk_warn_fraction": (
        "Fraction (0-1) of the CTE file-tier capacity that triggers a disk-space warning; lower "
        "for earlier warnings before the tier fills."
    ),
    "arc.cte.file_capacity": (
        'Capacity string (e.g. "50GB") for clio-core\'s disk-backed file storage tier; raise if '
        "the working set needs more durable disk space."
    ),
    "arc.cte.ram_capacity": (
        'Hard byte ceiling (e.g. "1GB") for clio-core\'s RAM working arena; raise for more '
        "headroom -- a deliberate desktop cap on DRAM growth."
    ),
    "arc.events_chunk_segments": (
        "Semantic-event segments held per on-disk chunk of a session's event log before rolling; "
        "raise to cut chunk count, lower to bound re-encode cost."
    ),
    "arc.liveness.backoff_initial_s": (
        "Seconds of initial backoff before the first retry of a stalled (zombie) clio-core RPC; "
        "raise to give a slow daemon more recovery time."
    ),
    "arc.liveness.backoff_max_s": (
        "Seconds cap on the growing backoff delay between stalled-RPC retries; lower to fail "
        "faster overall, raise to tolerate longer hiccups."
    ),
    "arc.liveness.retries": (
        "Retry attempts after a clio-core RPC stalls (no response) before the store is quarantined "
        "as runtime-lost; raise for a flakier daemon."
    ),
    "arc.liveness.stall_after_s": (
        "Seconds one clio-core RPC may run with no response before it's treated as a stalled "
        "zombie peer; raise for very large blob ops."
    ),
    "arc.lsm_compaction_threshold": (
        "Memtable flushes accumulated before the ARC metrics LSM tree compacts into one file; "
        "raise to compact less often, lower for eager compaction."
    ),
    "arc.lsm_memtable_size": (
        "Max entries in the ARC metrics LSM tree's in-memory memtable before it flushes to disk; "
        "raise to batch more writes, lower to bound memory."
    ),
    "arc.server_conf": (
        "Path to a clio-core server YAML config the port-resolution logic reads; set to point "
        "liveness probing at a non-default config file."
    ),
    "arc.store": (
        'Selects the ARC persistence backend: "cte" (clio-core, default) or "local" (plain '
        'files); use "local" for guaranteed on-disk durability.'
    ),
    "arc.store_config": (
        'Path to the clio-core CTE config used when arc.store is "cte"; set to point ARC at a '
        "hand-authored multi-tier topology."
    ),
    "artifacts.cas_budget_bytes": (
        "Byte budget for the content-addressed artifact store; over it, GC evicts unreachable "
        "blobs oldest-first. Raise to retain more history."
    ),
    "artifacts.cas_max_file_bytes": (
        "Per-file byte ceiling for CAS ingestion; larger files stay workspace-referenced only, not "
        "copied. Raise to CAS-ingest bigger deliverables."
    ),
    "artifacts.export_license": (
        "SPDX license string stamped on an RO-Crate export's root Dataset (default NOASSERTION = "
        "none declared); set to your org's license."
    ),
    "artifacts.hash_max_file_bytes": (
        "Byte ceiling on hashing a designated output at mint time; larger files are stat-pinned "
        "(no sha256) to avoid multi-GB reads on the turn thread."
    ),
    "artifacts.hash_stat_cache": (
        "Whether to trust a blob's stat (size+mtime) instead of re-hashing to confirm a CAS blob "
        "is intact; keep false, Windows mtimes are unreliable."
    ),
    "artifacts.instrument_arg_max_bytes": (
        "Byte ceiling per tool-call arg kept in a TransformRecord's instrument args before elision "
        "to a digest; raise to keep more arg detail."
    ),
    "artifacts.instrument_total_max_bytes": (
        "Whole-instrument byte ceiling on a TransformRecord's combined tool-call args; over it "
        "every arg collapses to one digest instead."
    ),
    "artifacts.lineage_max_nodes": (
        "Node cap on a COMPLETE artifact-lineage closure (export/reproduce, not the interactive "
        "/lineage view); raise for deeper dependency chains."
    ),
    "artifacts.proposals_batch_max": (
        "Max artifact proposals in one create_artifact call; an oversized batch is rejected whole "
        "instead of emitting many events."
    ),
    "artifacts.proposals_per_turn": (
        "Ceiling on new artifact promotions one turn may make via create_artifact; re-designating "
        "identical bytes is free."
    ),
    "artifacts.table_preview_max_rows": (
        "Ceiling on rows returned by one artifact table-preview response; raise for a denser "
        "chart, lower to shrink the JSON payload."
    ),
    "artifacts.table_preview_max_source_bytes": (
        "Largest CSV artifact, in bytes, the table-preview route will read; the file streams twice "
        "so this bounds latency, not memory."
    ),
    "autocompact.pct": (
        "Fraction (0-1) of the model's context window that triggers proactive auto-compaction; "
        "lower to compact earlier, raise to accumulate more."
    ),
    "debug.dump_unparseable": (
        "Filesystem path to dump raw LM completions the adapter failed to parse; set when "
        "diagnosing a model/adapter mismatch, else a no-op."
    ),
    "debug.level": (
        'Log verbosity ("off"/"low"/"med"/"high", default "low"); raise to "high" for '
        'per-call firehose logs, drop to "off" for quiet.'
    ),
    "debug.lm_response": (
        "Legacy toggle that force-enables the lm_response trace tag; turn on to log every raw LM "
        "response without raising debug.level."
    ),
    "debug.memprof": (
        "Switches the SIGUSR1 signal from a thread-traceback dump to a tracemalloc heap snapshot; "
        "enable when hunting a memory leak."
    ),
    "debug.memprof_frames": (
        "Stack depth (frames) tracemalloc records per allocation when debug.memprof is on; raise "
        "for finer leak attribution, at more overhead."
    ),
    "debug.memprof_out": (
        "File stem for numbered memprof snapshot dumps; unset (default) writes each SIGUSR1 dump "
        "to stderr instead."
    ),
    "debug.only": (
        "Comma-separated tag whitelist overriding debug.level, emitting only listed trace tags; "
        "use for surgical logging of one code path."
    ),
    "debug.sse_event_log": (
        "Filesystem path to append one JSON line per SSE event sent to any session; set to "
        "audit/replay the exact event sequence served."
    ),
    "debug.sse_wire_tap": (
        "Filesystem path to append the raw bytes written to an SSE connection; set for a "
        "byte-for-byte capture of what a client received."
    ),
    "debug.stream_audit_log": (
        "Filesystem path for JSONL stream-audit records (SSE scheduling, transcript events, "
        "receive times); set to diagnose streaming issues."
    ),
    "gact.auth.bearer_token": (
        "Optional bearer token required on non-loopback GACT API requests; set to protect a server "
        "exposed beyond localhost."
    ),
    "gact.blueprint_registry.url": (
        "Git URL of the default agent-blueprint marketplace registry; override to point at a "
        "private/mirrored marketplace."
    ),
    "gact.blueprint_source.clone_timeout_s": (
        "Seconds a temporary `git clone --depth 1` of a remote blueprint source may run before "
        "timing out; raise for a large repo or slow link."
    ),
    "gact.cancellation_grace_s": (
        "Seconds a cooperative session cancel is given before the turn task is hard-cancelled; "
        "raise to let a mid-tool-call turn unwind cleanly."
    ),
    "gact.cors.origins": (
        "Comma-separated browser origins allowed to call the GACT API cross-origin; set when "
        "serving a web UI from a different origin."
    ),
    "gact.ledger_retention.a2ui_messages.max": (
        "Retention bound on one A2UI surface's message log; the oldest non-createSurface message "
        "is evicted first past it."
    ),
    "gact.ledger_retention.command_audit.max": (
        "Max entries kept in the in-memory command-audit ledger before the oldest is FIFO-evicted; "
        "raise for more in-memory audit history."
    ),
    "gact.ledger_retention.context_frames.max": (
        "Max per-session context-frame entries kept in memory before FIFO eviction; raise to "
        "retain more attached-context history."
    ),
    "gact.ledger_retention.memory_tool_audit.max": (
        "Max entries kept in the in-memory memory-tool-audit ledger before FIFO eviction; raise "
        "for more memory-tool audit history."
    ),
    "gact.ledger_retention.pending_diffs.hard": (
        "Absolute ceiling on the pending-diffs ledger; past this even a still-pending diff is "
        "force-evicted oldest-first."
    ),
    "gact.ledger_retention.pending_diffs.max": (
        "Soft cap on the pending-diffs ledger where terminal (resolved/applied/rejected) entries "
        "evict first, protecting pending diffs."
    ),
    "gact.ledger_retention.permissions.hard": (
        "Absolute ceiling on the in-memory permissions ledger; past this even a still-pending "
        "request is force-evicted oldest-first."
    ),
    "gact.ledger_retention.permissions.max": (
        "Soft cap on the permissions ledger where terminal (resolved) entries evict first, "
        "protecting still-pending approval requests."
    ),
    "gact.ledger_retention.shared_tokens.hard": (
        "Absolute ceiling on the shared-token ledger; past this even a still-pending token is "
        "force-evicted oldest-first."
    ),
    "gact.ledger_retention.shared_tokens.max": (
        "Soft cap on the shared-token ledger where terminal entries evict first, protecting "
        "still-pending shared tokens."
    ),
    "gact.ledger_retention.turn_attempts.hard": (
        "Absolute ceiling on the turn-attempts ledger; past this even a still-pending attempt is "
        "force-evicted oldest-first."
    ),
    "gact.ledger_retention.turn_attempts.max": (
        "Soft cap on the turn-attempts ledger where terminal entries evict first, protecting "
        "still-pending turn attempts."
    ),
    "gact.live_edge_streaming": (
        "Experimental flag enabling live-edge SSE atom sealing (default off); only meaningful with "
        "the S5 atoms regime, leave off normally."
    ),
    "gact.loop_inbox.max_events": (
        "Per-session bound on buffered mid-turn wakes (child completions and user steers) before "
        "the oldest recoverable one is evicted; raise for very fan-out-heavy turns."
    ),
    "gact.message_intents.max_acceptances_per_session": (
        "Per-session cap on retained message-acceptance records used for idempotent POST replay; "
        "raise for clients that retry over long windows, lower to shrink the intent store."
    ),
    "gact.message_intents.max_queued_per_session": (
        "Per-session cap on durable queued (future) messages; a create past it is REFUSED with a "
        "typed 429, never evicted. Raise for heavy queue use."
    ),
    "gact.message_intents.max_settled_steers_per_session": (
        "Per-session cap on retained SETTLED (consumed/cancelled) pending-steer rows; undelivered "
        "steers are never evicted. Lower to shrink the intent store."
    ),
    "gact.resident_ledgers.idle_ttl_s": (
        "Seconds an idle session's in-memory transcript ledger may sit resident before release "
        "(rehydrates on next access); lower to free memory."
    ),
    "gact.resident_ledgers.max_bytes": (
        "Byte cap on the total resident transcript-ledger cache across all sessions; raise on a "
        "host with more RAM to keep more sessions warm."
    ),
    "gact.resident_ledgers.max_sessions": (
        "Max sessions whose transcript ledgers stay resident in memory at once; raise to keep more "
        "sessions warm at the cost of memory."
    ),
    "goal.judge_model": (
        "Model id for the separate cheap LLM-judge that evaluates an armed goal condition; set a "
        "small/cheap model to keep judging inexpensive."
    ),
    "hooks.allow_managed_only": (
        "Lockdown flag dropping every non-managed (user/project) hook source, keeping only "
        "admin-managed hooks; enable for a locked-down deployment."
    ),
    "hooks.config": (
        "Explicit single hook-config file overriding the normal user+project discovery paths; set "
        "for tests or single-file deployments."
    ),
    "hooks.defer_timeout": (
        "Seconds a parked PreToolUse defer waits for an out-of-band resolution before failing safe "
        "to deny; raise to give an approver more time."
    ),
    "hooks.managed_config": (
        "Path to the admin/managed hook config file, highest precedence and opt-in (no default "
        "location); set to enable managed hooks."
    ),
    "hooks.stop_loop_cap": (
        "Hard ceiling on Stop-hook-driven turn re-drives within one stop-sequence; raise to allow "
        "more self-correction loops before settling."
    ),
    "hooks.trust_store": (
        "Override path for the trusted hook-fingerprint store; set to relocate where hook trust "
        "decisions persist."
    ),
    "limits.codex_sdk_progress_timeout_s": (
        "Max silence (seconds) for one Codex SDK exchange/event, resetting on every progress event "
        "rather than a fixed clock; raise for long turns."
    ),
    "limits.context_inline_bytes": (
        "Byte cap per attached file inlined into context injection; raise to inline larger "
        "attachments, lower to bound prompt growth."
    ),
    "limits.empty_tool_repair_attempts": (
        "Bounded retries when a ReAct response omits the required tool-call field; raise for "
        "models that often emit malformed tool-less output."
    ),
    "limits.extract_repair_attempts": (
        "Bounded independent re-samples after a structured-output schema-repair failure; raise for "
        "models needing more attempts to recover."
    ),
    "limits.fs_read_bytes": (
        "Byte cap on a single read_file tool call; raise to read larger files whole, lower to "
        "bound memory and token cost."
    ),
    "limits.lm_call_s": (
        "Hard ceiling (seconds) an in-flight LM call is trusted as progress before the watchdog "
        "assumes it's wedged; raise for slow long-output models."
    ),
    "limits.lm_inter_token_idle_s": (
        "When streaming, seconds a call may go without a new token before it's stalled; raise to "
        "tolerate slower generation, lower to catch it faster."
    ),
    "limits.lm_parse_retry_attempts": (
        "Overrides re-sample attempts after an unrecoverable structured-output parse failure; "
        "reasoning models default to 2, others 0."
    ),
    "limits.lm_transient_backoff_s": (
        "Seconds to wait before re-issuing an LM call after a transient provider failure; default "
        "8s lets LM Studio JIT-reload a crashed model."
    ),
    "limits.lm_transient_retries": (
        "Bounded retry count for a transient (non-parse) provider failure before giving up; raise "
        "on a flaky provider connection."
    ),
    "limits.mcp_content_block_max_bytes": (
        "Byte cap on one MCP content block's decoded binary payload (image/audio/resource) before "
        "it's elided; raise for larger images."
    ),
    "limits.mcp_reconnect_timeout_s": (
        "Seconds bounding the connect+list-tools round-trip when reconnecting an MCP server; raise "
        "for a slow-starting server."
    ),
    "limits.model_tool_result_chars": (
        "Character bound on the model-facing MCP tool-result projection (head/tail cut); distinct "
        "from limits.tool_result_chars."
    ),
    "limits.shell_default_output_bytes": (
        "Default byte cap on shell-command stdout/stderr when the caller specifies none; raise for "
        "commands with verbose output."
    ),
    "limits.shell_default_timeout_s": (
        "Default wall-clock seconds a shell command may run when the caller specifies no timeout; "
        "raise for slower default commands."
    ),
    "limits.shell_max_command_chars": (
        "Max character length of a shell command string the tool accepts; raise for scripts that "
        "assemble long commands."
    ),
    "limits.shell_max_output_bytes": (
        "Hard ceiling in bytes on shell-command output the tool will ever return, regardless of a "
        "caller cap; raise for large output."
    ),
    "limits.shell_max_timeout_s": (
        "Hard ceiling in seconds on the timeout a shell tool call may request; raise for "
        "legitimately long-running shell operations."
    ),
    "limits.submit_repair_attempts": (
        "Bounded forced-resubmit re-asks when a ReActV2 loop ends with declared outputs missing; "
        "raise for models needing more nudges to finish."
    ),
    "limits.tool_result_chars": (
        "Character bound on the transcript/evidence-metadata preview of a tool result; raise to "
        "keep more result text visible in the transcript."
    ),
    "limits.transient_provider_retry_delays": (
        "Comma-separated backoff delays (seconds) between retries of a transient provider error; "
        "set empty to disable these retries."
    ),
    "limits.turn_timeout_s": (
        "Seconds a turn may run with no progress before it is timed out; raise for long-running "
        "turns, lower to fail stuck turns faster."
    ),
    "lm.api_base": (
        "Overrides the LM provider's default API base URL; set to point at a non-default endpoint "
        "for a provider (custom LM Studio/OpenAI host)."
    ),
    "lm.claude_code_transport": (
        'Selects the Claude Code transport; "sdk" is the only supported value, kept as an '
        "explicit contract check, not a tuning knob."
    ),
    "lm.codex_transport": (
        'Selects the Codex transport; "sdk" is the only supported value, kept as an explicit '
        "contract check, not a tuning knob."
    ),
    "lm.defer_tiktoken": (
        "Defers litellm's ~40MB cl100k tiktoken vocab load until first real encode; disable if "
        "something depends on eager tiktoken load at boot."
    ),
    "lm.disable_json_adapter_fallback": (
        "Force-disables the JSON-adapter fallback for cloud providers that reject response_format; "
        "set true if a provider 400s on it."
    ),
    "lm.disable_thinking": (
        'Turns off reasoning/"thinking" sampling for the active LM; set true to force a '
        "reasoning-capable model into non-reasoning mode."
    ),
    "lm.guided_output": (
        "Switches to schema-constrained JSON output instead of the text ChatAdapter; enable "
        "per-model when a reasoning model drops required fields."
    ),
    "lm.lmstudio_flash_attention": (
        "Requests flash attention on LM Studio model loads, cutting KV-cache memory use; disable "
        "only if it itself causes a load failure."
    ),
    "lm.max_tokens": (
        "Overrides the per-reply output token cap; raise for models needing longer completions, "
        "else the provider/handshake default applies."
    ),
    "lm.min_p": (
        "Sets the min-p sampling param (via extra_body on llama.cpp/LM Studio); tune for reasoning "
        "models needing fuller sampling than temp-0."
    ),
    "lm.model": (
        "Pins the exact model identifier to use; set when the provider default model isn't the one "
        "you want."
    ),
    "lm.planner_max_tokens": (
        "Token cap for the lower-temperature planner/routing generations; raise if planner JSON "
        "output gets truncated."
    ),
    "lm.planner_temperature": (
        "Sampling temperature for deterministic action-planning calls (default 0.3, forced 0.0 for "
        "local reasoning profiles); lower for determinism."
    ),
    "lm.presence_penalty": (
        "Sets the OpenAI-standard presence-penalty sampling parameter; tune for reasoning models "
        "needing fuller sampling than temp-0."
    ),
    "lm.provider": (
        "Selects the LM backend (lm_studio, ollama, openai, anthropic, argonne, codex, "
        "claude_code); change to switch which provider clio talks to."
    ),
    "lm.reasoning_model": (
        "Forces/forbids reasoning-model behavior for the active model, overriding auto-detection; "
        "set when auto-detection misclassifies a model."
    ),
    "lm.stop_sequences": (
        "||-joined stop sequences for reasoning models to truncate output after the final field; "
        "override if a model's trace leaks past stop points."
    ),
    "lm.temperature": (
        "Sampling temperature for the main agentic LM calls; defaults to 0.0 since clio drives "
        "structured tool-call output, raise for creative sampling."
    ),
    "lm.thinking_budget": (
        "Explicit reasoning token-budget override, mapped per-provider (Anthropic/Claude Code "
        "SDK/OpenAI reasoning_effort); set for a fixed allowance."
    ),
    "lm.thinking_level": (
        "Provider-generic reasoning level (off/low/medium/high); set to raise or lower reasoning "
        'effort, or "off" to actively disable it.'
    ),
    "lm.top_k": (
        "Sets the top-k sampling param (via extra_body on llama.cpp/LM Studio); tune for reasoning "
        "models needing fuller sampling than temp-0."
    ),
    "lm.top_p": (
        "Sets the OpenAI-standard top-p sampling parameter; tune for reasoning models needing "
        "fuller sampling than the temp-0 default."
    ),
    "paths.data_dir": (
        "Base directory for the agent's on-disk data (ARC, sessions, etc.), default "
        '".clio/agent" under the workspace; relocates agent state.'
    ),
    "paths.model_catalog": (
        "Overrides the file path for the discovered-model catalog cache; set to relocate it off "
        "the default user-data directory."
    ),
    "paths.model_db": (
        "Overrides the file path for the writable per-model limits DB (context/output limit "
        "cache); set to relocate off the default data dir."
    ),
    "paths.sessions": (
        "Full override path for the sessions.json registry file; unset defaults to "
        "`<workspace>/.clio/agent/sessions.json`."
    ),
    "paths.web_dir": (
        "Directory of the built web-UI bundle to serve; unset (default) disables web mode and "
        "keeps the server headless/TUI-only."
    ),
    "permissions.ai_review_timeout_s": (
        "Seconds the AI-review permission gate waits for a reviewer LM verdict before failing safe "
        "to human escalation; lower to fail fast."
    ),
    "provenance.agentic.flowcept.campaign_id": (
        "Free-text campaign label attached to every Flowcept provenance record; set to group runs "
        "under one campaign."
    ),
    "provenance.agentic.flowcept.campaign_scope": (
        'Whether a Flowcept campaign spans one session or the whole process ("session" default); '
        "switch for many sessions as one campaign."
    ),
    "provenance.agentic.flowcept.check_safe_stops": (
        "Whether Flowcept validates safe-stop points before treating a workflow as complete; "
        "disable only where the check misfires."
    ),
    "provenance.agentic.flowcept.exclude_events": (
        "Glob patterns of event types Flowcept drops (default excludes noisy per-token deltas and "
        "thinking events); widen to cut export volume."
    ),
    "provenance.agentic.flowcept.include_events": (
        'Glob patterns of event types Flowcept exports (default "*" = everything not excluded); '
        "narrow to scope provenance to specific events."
    ),
    "provenance.agentic.flowcept.privacy": (
        'Flowcept payload privacy level ("metadata" default, no raw content); relax only when '
        "the Flowcept backend is trusted with full content."
    ),
    "provenance.agentic.flowcept.workflow_scope": (
        'Whether a Flowcept workflow record spans one session or the process ("session" '
        "default); change to correlate sessions as one workflow."
    ),
    "provenance.agentic.jsonl.path": (
        "Filesystem path for the JSONL provenance log; falls back to trace.path then the workspace "
        "default -- redirects provenance output."
    ),
    "provenance.agentic.providers": (
        'Comma-separated agentic provenance providers to run (default ["jsonl"]); add '
        '"flowcept" for external tooling, or clear to disable.'
    ),
    "provenance.agentic.query_default": (
        "Which provenance provider name /v1/provenance queries answer from by default "
        '("native"); change to make Flowcept the default source.'
    ),
    "provenance.agentic.queue_size": (
        "Max buffered events in the provenance dispatcher's async queue before backpressure; raise "
        "on high event-rate sessions."
    ),
    "provenance.artifacts.cmf.artifact_root": (
        "Filesystem path where the CMF artifact-provenance backend stores artifact data (default "
        "under the app data dir); relocates the store."
    ),
    "provenance.artifacts.cmf.artifact_store": (
        "CMF backend's storage mode for artifact bytes (default 'reference': tracks by path, no "
        "duplication); rarely changed."
    ),
    "provenance.artifacts.cmf.metadata_path": (
        "Filesystem path to the CMF metadata SQLite database (default under the app data dir); set "
        "to relocate or share the store."
    ),
    "provenance.artifacts.cmf.pipeline_name": (
        "Pipeline name CMF records against tracked artifacts (default 'clio-agent'); change to "
        "separate deployments sharing one CMF store."
    ),
    "provenance.artifacts.cmf.publish_timeout_s": (
        "Seconds the CMF backend waits for a publish call to a CMF server to complete; raise if "
        "publishing to a remote server times out."
    ),
    "provenance.artifacts.cmf.python": (
        "Path of a LOCAL interpreter that can import cmflib, for the optional local-worker write "
        "mode; unsupported on Windows. Prefer server_url, which needs no local CMF."
    ),
    "provenance.artifacts.cmf.server_url": (
        "URL of the CMF metadata server to write artifact provenance to. Set this alone for the "
        "supported deployment: it needs no local CMF runtime on any client OS."
    ),
    "provenance.artifacts.cmf.worker_url": (
        "Reserved for a future in-stack CMF write service; setting it is refused today. Use "
        "server_url."
    ),
    "provenance.artifacts.include_events": (
        "Which artifact event types (created, version.added, proposed, ...) reach the selected "
        "provenance provider; narrow to cut writes."
    ),
    "provenance.artifacts.native.storage": (
        "Storage backend for the built-in (non-CMF) artifact-provenance provider; only 'file' is "
        "supported today."
    ),
    "provenance.artifacts.provider": (
        "Which backend records artifact lineage: 'native' (built-in) or 'cmf' (Common Metadata "
        "Framework); switch to integrate with CMF."
    ),
    "provenance.artifacts.queue_size": (
        "Max queued artifact-provenance events awaiting async dispatch; raise if a slow provider "
        "causes drops under bursty activity."
    ),
    "providers.claude_code.max_concurrent_processes": (
        "Process-wide cap on concurrently-connected claude CLI subprocesses; a connect beyond it "
        "waits, raise for more concurrent sessions."
    ),
    "providers.claude_code.session_reuse": (
        "Keeps a pooled/reused SDK connection per scope instead of a fresh client per call; set "
        "false to restore pre-#891 fresh-connect behavior."
    ),
    "providers.claude_code.stateful_capacity": (
        "Max live Claude Code stateful-session entries before LRU eviction; raise on a host "
        "running many concurrent stateful sessions."
    ),
    "providers.claude_code.stream_idle_ttl_s": (
        "Seconds a scope-keyed pooled Claude Code connection may sit idle before the next request "
        "reaps it; lower to free idle connections sooner."
    ),
    "providers.codex.credential_home_capacity": (
        "Max simultaneous private CODEX_HOME credential-dir copies the Codex SDK transport keeps "
        "alive; raise for many concurrent Codex sessions."
    ),
    "resources.delivery_ledger_max_records": (
        "Rows of resource-delivery provenance kept in resource_deliveries.json before the oldest "
        "are compacted away; raise to retain a longer attachment audit trail."
    ),
    "resources.derivative_name_max_chars": (
        "Longest derivative id stored under its own filename before it is hashed to a short "
        "digest name; lower on Windows deployments with deep state directories (MAX_PATH)."
    ),
    "resources.document_processor_url": (
        "Base URL of the optional document-processing service used to derive structured views of "
        "uploaded resources; unset leaves resources served as originals."
    ),
    "resources.list_max_records": (
        "Resource rows one workspace listing returns before it reports truncation; raise for "
        "workspaces holding many uploads, lower to keep tool results small."
    ),
    "resources.max_bytes": (
        "Byte ceiling on a single uploaded workspace resource; raise for large scientific inputs, "
        "lower to bound per-workspace disk use."
    ),
    "resources.processor_cancel_timeout_s": (
        "Seconds to wait for the document processor to acknowledge a cancellation; raise only if "
        "the service cancels slowly under load."
    ),
    "resources.processor_connect_timeout_s": (
        "Seconds to wait for a TCP connection to the document processor; raise on a slow or "
        "congested link to the service."
    ),
    "resources.processor_pool_timeout_s": (
        "Seconds to wait for a free connection from the document-processor client pool; raise "
        "when many uploads convert concurrently."
    ),
    "resources.processor_read_timeout_s": (
        "Seconds to wait for the document processor's response headers/body on a submit; raise "
        "when the service queues submissions behind long conversions."
    ),
    "resources.processor_status_timeout_s": (
        "Seconds to wait for one document-processor status poll; raise only if status responses "
        "are genuinely slow, since every poll pays it."
    ),
    "resources.processor_write_timeout_s": (
        "Seconds to stream one upload body to the document processor; 0 derives it from "
        "resources.max_bytes at 1 MiB/s (floor 60s) so a raised ceiling is not cut off mid-body."
    ),
    "resources.search_excerpt_chars": (
        "Characters of each matching line returned by a bounded resource search; raise for more "
        "context per hit, lower to shrink tool output."
    ),
    "resources.search_match_limit": (
        "Matches one bounded resource search returns before reporting truncation; raise for "
        "broader sweeps, lower to keep tool results small."
    ),
    "resources.status_poll_failure_threshold": (
        "Consecutive failed converter status polls tolerated before the resource is marked failed "
        "with converter_status_unavailable; lower to give up on a vanished converter sooner."
    ),
    "resources.structure_node_max_bytes": (
        "Byte ceiling on ONE structured node (a page, table, picture or text block) served from a "
        "derived view; raise for documents with very large single nodes."
    ),
    "resources.text_preview_bytes": (
        "Byte ceiling on a text resource served inline through the preview route; raise to "
        "preview larger documents in a client."
    ),
    "resources.text_read_chars": (
        "Characters returned by one bounded resource text read before truncation; raise to hand "
        "the model more of a document per call."
    ),
    "resources.text_scan_bytes": (
        "Byte ceiling on text CLIO will linearly scan for a resource search or direct read; raise "
        "for larger plain-text inputs, lower to bound per-call CPU."
    ),
    "resources.upload_chunk_bytes": (
        "Largest single resumable-upload chunk the server accepts, enforced while the body "
        "streams; raise for fewer round-trips on fast links, lower to bound per-request memory."
    ),
    "relay.cluster": (
        "This deployment's registered relay cluster identity; set it to route jarvis/remote-MCP "
        "tool calls to a specific cluster."
    ),
    "relay.console.enabled": (
        "Whether the live job console tail folds into the task record; disable to stop the "
        "periodic console pull entirely."
    ),
    "relay.console.pull_limit_bytes": (
        "Bytes requested per poll of a relay job's console log (clamped to relay's 1 MiB cap); "
        "raise to refill a window faster after a gap."
    ),
    "relay.console.stream": (
        'Which relay log stream ("console", "stdout", "stderr") the console fold pulls from; '
        "switch to stdout/stderr only for diagnostics."
    ),
    "relay.console.tail_cap_bytes": (
        "Byte cap on the rolling console tail kept on the task record for UI display; raise for "
        "more scrollback, lower to shrink records."
    ),
    "relay.fetch_max_bytes": (
        "Max artifact size in bytes this deployment transfers inline via relay_fetch_artifact; "
        "raise for larger downloads, lower to block big ones."
    ),
    "relay.http_url": (
        "Relay job/artifact HTTP endpoint URL; set together with relay.mcp_url to enable the relay "
        "transport."
    ),
    "relay.install_surface.attention_idle_seconds": (
        'Seconds of no subprocess output before a relay CLI job is relabeled "needs operator '
        'attention" (e.g. SSH/2FA); tune for slow networks.'
    ),
    "relay.install_surface.bounded_timeout_seconds": (
        "Timeout in seconds for a fast, non-SSH-dialing clio-relay CLI sub-probe "
        "(register/doctor/status); raise if calls time out on a slow host."
    ),
    "relay.install_surface.cli_path": (
        "Explicit path to the deployed clio-relay executable, overriding PATH discovery; set when "
        "it's installed somewhere non-standard."
    ),
    "relay.install_surface.job_retention_hard_cap": (
        "Hard ceiling on tracked relay CLI jobs before even a running one is force-evicted; raise "
        "on a box driving many concurrent relay ops."
    ),
    "relay.install_surface.job_retention_max": (
        "Soft cap on tracked relay CLI jobs; past this the oldest terminal job is evicted first to "
        "bound memory."
    ),
    "relay.install_surface.long_operation_timeout_seconds": (
        "Runaway timeout (seconds) for SSH-dialing relay CLI ops (bootstrap, session "
        "start/attach/teardown); raise for slow SSH targets."
    ),
    "relay.install_surface.output_tail_bytes": (
        "Byte cap on the retained stdout/stderr tail from a relay CLI subprocess call; raise for "
        "more diagnostic context on failures."
    ),
    "relay.install_surface.parsed_document_max_bytes": (
        "Byte buffer size for parsing a whole clio-relay CLI JSON document; raise if a large "
        "session document is failing to parse."
    ),
    "relay.jarvis_door_namespace": (
        'The relay-registered JARVIS tool-name namespace prefix; set to "" to use relay\'s older '
        "compact-name door instead."
    ),
    "relay.mcp_url": (
        "Relay MCP control endpoint URL; set together with relay.http_url to enable relay tool "
        "discovery."
    ),
    "relay.owner_session_generation_id": (
        "Generation id paired with relay.owner_session_id identifying this deployment's owned "
        "relay session; set both together or neither."
    ),
    "relay.owner_session_id": (
        "This deployment's owned relay HTTP-API session id, required with "
        "owner_session_generation_id when the relay door is session-bound."
    ),
    "relay.remote_agent.mcp_config_path": (
        "Path to the MCP config file used by a remote relay-placed agent invocation; set to give "
        "spawned agents a specific tool config."
    ),
    "relay.remote_agent.model": (
        "Model identifier used for agents spawned onto a relay cluster; override to run remote "
        "relay agents on a non-default model."
    ),
    "relay.remote_agent.prompt_path": (
        "Path to the prompt file for a relay-cluster-placed remote agent; required (with the "
        "cluster) for relay agent spawning to work."
    ),
    "relay.remote_agent.workdir": (
        "Working directory used for a relay-placed remote agent invocation; set to point remote "
        "execution at a specific cluster directory."
    ),
    "relay.tool_surfaces_ttl_seconds": (
        "Seconds a discovered relay tool catalog is trusted before re-discovery; lower for faster "
        "pickup of new tools, raise to cut overhead."
    ),
    "runtime.api_base": (
        "Base URL of a running gact API used by the doctor/status live health probe; empty "
        "(default) skips that probe."
    ),
    "runtime.capture_reasoning": (
        "Whether per-call reasoning/chain-of-thought traces are persisted onto assistant message "
        "metadata; disable to reduce metadata growth."
    ),
    "runtime.environment": (
        'Deployment environment label ("dev" default) threaded into LM config; change to reflect '
        "staging/prod for environment-aware logging."
    ),
    "runtime.live_streaming": (
        "Streams the top-level GACT turn's answer live via dspy.streamify instead of blocking; "
        "disable per-model if streaming responses break."
    ),
    "runtime.lm_token_liveness": (
        "Streams expert LM calls token-by-token so each token refreshes the no-progress watchdog; "
        "disable only if streaming plumbing misbehaves."
    ),
    "sandbox.enabled": (
        "Whether tool-execution sandboxing/confinement is applied; disable only for trusted local "
        "dev where sandbox setup gets in the way."
    ),
    "scheduler.jitter_window_s": (
        "Seconds-wide window a schedule's fire time is deterministically jittered within; raise to "
        "spread schedules sharing one provider quota."
    ),
    "scheduler.max_lifetime_s": (
        'Seconds after creation a recurring schedule with no explicit "until" is auto-retired; '
        "raise to let schedules run longer unattended."
    ),
    "scheduler.max_retries": (
        "Consecutive failed fires tolerated before a schedule is disabled with a typed reason; "
        "raise to tolerate more transient failures."
    ),
    "scheduler.min_interval_s": (
        "Floor on how often any recurring schedule may fire; lower only if sub-minute firing is "
        "genuinely needed (60s = cron's finest grain)."
    ),
    "scheduler.timezone": (
        "Default timezone schedules resolve their cron expression's wall-clock fire times in; set "
        "to change the default zone new schedules use."
    ),
    "spotter.clearance_progress_timeout_s": (
        "No-progress window (seconds) between observable SPOTTER watcher signals before a "
        "protected tool call fails closed; active checks never trip it."
    ),
    "spotter.max_clearance_events": (
        "Retention bound on the per-session SPOTTER clearance-event map; raise only for many "
        "concurrent protected sessions."
    ),
    "spotter.watcher_blueprint_id": (
        "Agent-Blueprint id used to build the SPOTTER standing watcher child session; change to "
        "point spotter-ai at a custom watcher blueprint."
    ),
    "spotter.watcher_expert_id": (
        "Expert id within the watcher blueprint SPOTTER arms as the standing watcher; change "
        "alongside watcher_blueprint_id for a custom expert."
    ),
    "tools.file_policy.allow_symlinks": (
        "Whether tool file reads/writes may traverse symlinks; set true only if a workflow "
        "legitimately relies on symlinked paths."
    ),
    "tools.file_policy.allowed_roots": (
        "Root directories every tool file read/write must resolve inside (default: cwd + system "
        "temp dir); add a root to grant access."
    ),
    "tools.file_policy.max_file_size_bytes": (
        "Byte-size cap (accepts K/M/G/T suffixes) on a file a read/write tool call may touch; "
        "raise to allow larger files."
    ),
    "tools.mcp.call_timeout_s": (
        "Runaway backstop seconds for one synchronous MCP tool call before it's abandoned; not the "
        "real per-tool clock -- raise only if tools hit it."
    ),
    "tools.mcp.cold_spawn_runaway_s": (
        "Generous backstop seconds for one MCP namespace's discovery/connect attempt before it's "
        "marked unreachable; raise for slow cold spawns."
    ),
    "tools.mcp.connect_mode": (
        'MCP protocol-era negotiation mode; "auto" probes modern then falls to legacy. Pin a '
        'version or "legacy" for a misbehaving server.'
    ),
    "tools.mcp.discovery_concurrency": (
        "Max declared MCP namespaces probed concurrently during boot discovery; raise to speed "
        "boot on a large fleet, lower to bound memory."
    ),
    "tools.mcp.discovery_heal_interval_s": (
        "Seconds between background re-probes of MCP namespaces that failed discovery; lower to "
        "recover faster, raise to cut retry noise."
    ),
    "tools.mcp.elicitation.url_trusted_origins": (
        "CSV allow-list of origins a url-mode elicitation may point to; add one to enable url "
        "elicitation for that server."
    ),
    "tools.mcp.input_required_max_rounds": (
        "Round cap on the modern-era InputRequiredResult retry loop for one tool call; raise for "
        "tools needing many follow-up inputs."
    ),
    "tools.mcp.launcher_cache_lock_timeout_s": (
        "Generous runaway backstop while waiting on the shared uv-launcher cache lock; fires only "
        "on a livelocked/unidentifiable holder."
    ),
    "tools.mcp.listing_ttl_h": (
        "Hours a cached MCP tool listing stays valid before a live relist is forced; lower to pick "
        "up upstream tool changes sooner."
    ),
    "tools.mcp.mount_retry_delays_s": (
        "Increasing waits (seconds, comma-separated) between an on-demand MCP mount's retry "
        "attempts; list length is the retry budget."
    ),
    "tools.mcp.probe_timeout_retries": (
        "Retries of the era-negotiation probe after a client-side timeout before giving up; raise "
        "for a slow-starting server."
    ),
    "tools.mcp.setup_timeout_s": (
        "Seconds allowed for an MCP tool executor's startup handshake; raise for servers with slow "
        "cold starts, lower to fail faster."
    ),
    "tools.mcp.spawn_diet": (
        "Enables the learned direct-interpreter spawn shortcut for clio-kit servers (skips ~90MB "
        "wrapper overhead); disable if it misbehaves."
    ),
    "tools.mcp.spawn_diet_ttl_h": (
        "Hours a learned spawn-diet shortcut plan stays valid before the launcher chain is "
        "relearned; lower to pick up upstream env changes sooner."
    ),
    "tools.mcp.workspace_max_resident": (
        "LRU cap on how many per-workspace MCP tool-executor fleets stay resident at once; raise "
        "to keep more workspaces warm."
    ),
    "tools.mcp.workspace_ttl_s": (
        "Idle seconds before an unused per-workspace MCP tool-executor fleet is closed; raise to "
        "keep fleets warm longer between calls."
    ),
    "tools.mcp_cache.max_age_days": (
        "Age ceiling in days for one built MCP uv-spawn environment before boot-time pruning "
        "evicts it; lower to reclaim disk sooner."
    ),
    "tools.mcp_cache.max_bytes": (
        "Total disk-size ceiling for the clio-owned MCP uv spawn cache, enforced at boot; raise on "
        "a machine with disk to spare."
    ),
    "tools.mcp_cache.temp_max_age_days": (
        "Age ceiling in days for stale pytest basetemp trees `clio doctor --gc` removes from the "
        "temp roots; lower to reclaim disk sooner."
    ),
    "tools.mcp_cache.temp_roots": (
        "Extra directories `clio doctor --gc` scans for stale pytest basetemp trees, beyond the "
        "system temp dir."
    ),
    "tools.shell.windows_backend": (
        "Which interpreter the Windows shell tool runs commands through (powershell/bash/cmd); "
        "match it to your commands' shell."
    ),
    "trace.detail_level": (
        "Verbosity of recorded semantic-trace events; raise for deeper post-hoc analysis, lower to "
        "shrink trace volume/storage."
    ),
    "trace.path": (
        "Legacy/shared filesystem path for the semantic trace backend, used as a fallback for "
        "provenance.agentic.jsonl.path when unset."
    ),
    "trace.semantic_config": (
        "JSON config blob (as a string) passed to a custom semantic-trace factory backend; only "
        "relevant with trace.semantic_factory set."
    ),
    "trace.semantic_factory": (
        "Import path of a custom Python factory supplying the semantic-trace backend; set only to "
        "replace the built-in jsonl/flowcept providers."
    ),
    "workflows.step_inactivity_s": (
        "No-activity window (seconds) before a declared-workflow step's child is judged stalled; a "
        "long but active step never trips it."
    ),
}
