# Knob Audit A — src/clio_agent/ configuration sweep

Config law: conf.resolve(key, env, default, cast); file > env > packaged defaults > in-code.
Packaged user-facing defaults: src/clio_agent/config.defaults.yaml.
conf.py-documented exemptions honored: bootstrap tier (CLIO_USER_DIR, CLIO_ENV_FILE,
CLIO_ENV_FILE_LOADED, XDG_CONFIG_HOME), secret tier (CLIO_LM_API_KEY, CLIO_ARGONNE_TOKEN,
ALCF_INFERENCE_TOKEN, CLIO_RELAY_API_TOKEN, CLIO_CRED_*), provider auth-status probes
(gact/routes/providers.py).

## OK_UNREFLECTED
OK_UNREFLECTED | provenance.agentic.flowcept.settings_path | gact/provenance/factory.py:130 | FLOWCEPT_SETTINGS_PATH | resolves correctly but dotted key absent from config.defaults.yaml (env alias is the non-CLIO passthrough FLOWCEPT_SETTINGS_PATH)
OK_UNREFLECTED | scheduler.tz | gact/scheduler.py:236 | TZ | secondary tz fallback (env=TZ); scheduler.timezone is documented but scheduler.tz is not in defaults
OK_UNREFLECTED | tools.mcp.elicitation.agent_audience.answer_mode | gact/agent_elicitation.py:271 | CLIO_MCP_ELICITATION_AGENT_AUDIENCE_ANSWER_MODE | sibling agent_audience.* keys are in defaults; this one is missing
OK_UNREFLECTED | tools.mcp.elicitation.agent_audience.default_unhinted | gact/agent_elicitation.py:291 | CLIO_MCP_ELICITATION_AGENT_AUDIENCE_DEFAULT_UNHINTED | sibling agent_audience.* keys are in defaults; this one is missing
OK_UNREFLECTED | tools.mcp.servers.{id}.probe_timeout_retries | tools/mcp_probe_hardening.py:110 | CLIO_MCP_PROBE_TIMEOUT_RETRIES__<ID> | dynamic per-server override key (interpolated), undiscoverable in defaults by construction; global tools.mcp.probe_timeout_retries IS documented

## ENV_SANCTIONED
ENV_SANCTIONED | CLIO_USER_DIR | paths.py:33 | CLIO_USER_DIR | bootstrap tier (explicitly exempted in conf.py docstring)
ENV_SANCTIONED | CLIO_ENV_FILE | config.py:144 | CLIO_ENV_FILE | bootstrap dotenv loader (exempted)
ENV_SANCTIONED | CLIO_LM_API_KEY | config.py:535 | CLIO_LM_API_KEY | secret tier (exempted)
ENV_SANCTIONED | CLIO_LM_API_KEY | providers/model_discovery/overlay.py:296 | CLIO_LM_API_KEY | secret tier (exempted) - fallback cloud key
ENV_SANCTIONED | <cloud provider key env> | providers/model_discovery/overlay.py:295 | e.g. OPENAI_API_KEY | secret tier - dedicated cloud API key env var
ENV_SANCTIONED | CLIO_ARGONNE_TOKEN / ALCF_INFERENCE_TOKEN | providers/credentials.py:95-96 | CLIO_ARGONNE_TOKEN, ALCF_INFERENCE_TOKEN | secret tier (exempted)
ENV_SANCTIONED | CLIO_CRED_<PROVIDER>_<ACCOUNT> | providers/credentials.py:167 | CLIO_CRED_* | secret tier (exempted) - named per-account credential
ENV_SANCTIONED | <cloud provider key env> | providers/credentials.py:172 | e.g. ANTHROPIC_API_KEY | secret tier - default cloud key
ENV_SANCTIONED | CLIO_ARGONNE_TOKEN / ALCF_INFERENCE_TOKEN | providers/handshake/argonne.py:95 | (via _TOKEN_ENV_VARS) | secret tier (exempted)
ENV_SANCTIONED | CLIO_ARGONNE_TOKEN / ALCF_INFERENCE_TOKEN | gact/routes/providers.py:536-537 | CLIO_ARGONNE_TOKEN, ALCF_INFERENCE_TOKEN | provider auth-status probe (exempted) + secret
ENV_SANCTIONED | ANTHROPIC_API_KEY/OPENAI_API_KEY/CLIO_LM_API_KEY | gact/routes/providers.py:170 | (probe) | provider auth-status presence probe (exempted)
ENV_SANCTIONED | env_key / CLIO_LM_API_KEY | gact/routes/providers.py:571 | (probe) | provider auth-status presence probe (exempted)
ENV_SANCTIONED | CLIO_RELAY_API_TOKEN | tools/relay_factory.py:235 | CLIO_RELAY_API_TOKEN | secret tier (exempted)
ENV_SANCTIONED | CLIO_RELAY_API_TOKEN | tools/relay_transport.py:207 | CLIO_RELAY_API_TOKEN | secret tier (exempted, RELAY_API_TOKEN_ENV)
ENV_SANCTIONED | XDG_DATA_HOME | providers/argonne_auth.py:197 | XDG_DATA_HOME | XDG bootstrap path (resolver/path machinery)
ENV_SANCTIONED | LOCALAPPDATA | providers/argonne_auth.py:191 | LOCALAPPDATA | OS path bootstrap (Windows data dir)
ENV_SANCTIONED | APPDATA / LOCALAPPDATA | runtime/sandbox_cli.py:252-253 | APPDATA, LOCALAPPDATA | OS path bootstrap (Windows roaming/local)
ENV_SANCTIONED | COMPUTERNAME/PROCESSOR_IDENTIFIER/OS/PROCESSOR_ARCHITECTURE | __init__.py:61-73 | (OS identity) | OS-provided host identity, not a clio knob
ENV_SANCTIONED | PROCESSOR_ARCHITECTURE | gact/provenance/flowcept.py:120 | (OS identity) | OS host identity, not a clio knob
ENV_SANCTIONED | ProgramFiles / ProgramFiles(x86) | gact/documents/renditions.py:83-84 | (OS path) | Windows install-path discovery, OS bootstrap
ENV_SANCTIONED | CODEX_HOME | providers/codex_credential_home.py:158 | CODEX_HOME | external-tool (Codex CLI) home path bootstrap for its auth file
ENV_SANCTIONED | CODEX_HOME | runtime/lm_provider_probe.py:94 | CODEX_HOME | external-tool Codex auth path bootstrap
ENV_SANCTIONED | CODEX_HOME | runtime/sandbox_codex.py:252 | CODEX_HOME | external-tool Codex home path bootstrap

## ENV_BARE
ENV_BARE | CLIO_ARC_STORE | arc/init_degradation.py:112 | CLIO_ARC_STORE | bare os.environ read of a knob that HAS a conf.resolve counterpart (arc.store, arc/storage.py:865); bypasses file layer - a config.yaml arc.store would not be seen here
ENV_BARE | CLIO_RUNTIME_STATE_DIR | arc/clio_core_config.py:79 | CLIO_RUNTIME_STATE_DIR | bare read for state dir; not on exemption list, no conf.resolve; arguably path-bootstrap but distinct from documented CLIO_USER_DIR
ENV_BARE | CHI_SERVER_CONF | arc/clio_core_liveness.py:145 | CHI_SERVER_CONF | bare read layered next to a resolved arc.server_conf; the sibling uses conf.resolve, this fallback does not
ENV_BARE | CLIO_ONLYOFFICE_URL | gact/documents/editors.py:192 | CLIO_ONLYOFFICE_URL | bare CLIO_* read in agent code, no conf.resolve, not exempted
ENV_BARE | CLIO_COLLABORA_URL | gact/documents/editors.py:194 | CLIO_COLLABORA_URL | bare CLIO_* read, no conf.resolve, not exempted
ENV_BARE | CLIO_GACT_PUBLIC_URL | gact/documents/editors.py:204 | CLIO_GACT_PUBLIC_URL | bare CLIO_* read with inline default http://host.docker.internal:8000; no conf.resolve
ENV_BARE | CLIO_ONLYOFFICE_JWT_SECRET | gact/documents/editors.py:243 | CLIO_ONLYOFFICE_JWT_SECRET | bare read; secret-ish but NOT on conf.py exemption list, so unsanctioned by the law as written
ENV_BARE | CLIO_DOCUMENT_TYPST_FONT | gact/documents/renditions.py:145 | CLIO_DOCUMENT_TYPST_FONT | bare CLIO_* read with inline default (Arial/DejaVu Serif); no conf.resolve
ENV_BARE | LM_STUDIO_API_TOKEN / LM_API_TOKEN | gact/providers/lmstudio.py:44-45 | LM_STUDIO_API_TOKEN, LM_API_TOKEN | bare provider-token reads; not on the exemption list (only CLIO_LM_API_KEY et al are); ambiguous but token=secret so borderline ENV_SANCTIONED
ENV_BARE | CLIO_PROVENANCE_PROVIDERS / CLIO_SEMANTIC_TRACE_BACKEND | provenance_config.py:34,40,112,118 | CLIO_PROVENANCE_PROVIDERS, CLIO_SEMANTIC_TRACE_BACKEND | reads env directly (paired with store().file_value) instead of conf.resolve; provenance.agentic.providers IS a resolved key elsewhere - this path re-implements the precedence by hand and reads the legacy trace.backend env with no resolve

## HARDCODED
HARDCODED | WAKEUP_MIN_S / WAKEUP_MAX_S | gact/autonomous_loop.py:58-59 | - | autonomous-loop wakeup delay clamp bounds (60s / 3600s), raw
HARDCODED | DEFAULT_INTERVAL_S | gact/autonomous_loop.py:63 | - | autonomous loop default interval 300s, raw
HARDCODED | DEFAULT_MAX_ITERS | gact/autonomous_loop.py:66 | - | autonomous loop iteration cap 100, raw
HARDCODED | DEFAULT_MAX_WALLCLOCK_S | gact/autonomous_loop.py:68 | - | autonomous loop wallclock cap 24h, raw
HARDCODED | DEFAULT_ELICITATION_TIMEOUT_S | gact/elicitation_bridge.py:83 | - | elicitation wait timeout 600s, raw default
HARDCODED | DEFAULT_MAX_GOAL_ITERS | gact/goal.py:70 | - | goal loop iteration cap 25, raw
HARDCODED | DEFAULT_MAX_GOAL_ITERS(replanning) STALL_CAP/STALL_THRESHOLD | gact/replanning.py:62,65 | - | replanning stall score cap 6 / threshold 3, operator-tunable heuristic bounds, raw
HARDCODED | _RETRY_BACKOFF_BASE_S / _RETRY_BACKOFF_CAP_S | gact/scheduler.py:85,88 | - | scheduler retry backoff base 60s / cap 3600s, raw (min_interval etc ARE resolved)
HARDCODED | _MAX_DRAIN_PASSES | gact/turn_runner.py:66 | - | turn message-drain pass cap 5, raw loop bound
HARDCODED | _HEALTH_PROBE_MAX_S | arc/rpc_liveness.py:66 | - | health re-probe window cap 10s, raw (stall/retry/backoff ARE resolved)
HARDCODED | _STALL_WATCH_MAX_WORKERS | arc/rpc_liveness.py:189 | - | stall-watch thread pool size 8, raw
HARDCODED | _RUNTIME_START_TIMEOUT_S | arc/storage.py:274 | - | clio-core runtime start wait 30s, raw
HARDCODED | _RPC_STALLED_RECOVERY_TTL_S | arc/clio_core_liveness.py:90 | - | RPC stalled-recovery TTL 30s, raw
HARDCODED | _DEFAULT_RUNTIME_PORT | arc/clio_core_liveness.py:77 | - | default clio-core runtime port 9413 (arc.core_port IS resolvable but this literal is the fallback default, not fed to resolve)
HARDCODED | _DEFAULT_BOOTSTRAP_TIMEOUT_S | gact/agent_blueprints.py:53 | - | blueprint registry git bootstrap timeout 20s, raw (used 3x)
HARDCODED | _OUTER_TIMEOUT_MARGIN_S / _CONTEXT_EXCERPT_MAX_CHARS | gact/agent_elicitation.py:206-207 | - | elicitation outer-timeout margin 15s / context excerpt 6000 chars, raw
HARDCODED | RELAY_PROBE_TIMEOUT_SECONDS | gact/relay_status.py:12 | - | relay TCP probe timeout 3s, raw
HARDCODED | _RELAY_DISCOVERY_FAILURE_TTL_SECONDS | gact/relay_wiring.py:37 | - | relay discovery negative-cache TTL 20s, raw (tool_surfaces_ttl IS resolved)
HARDCODED | _MAX_MODEL_CONTEXT_BYTES / _MAX_PRIVATE_RESULT_BYTES | gact/mcp_apps.py:62,61 | - | mcp-app model-context cap 128KiB / private-result cap 1MiB, raw
HARDCODED | _REGISTRY_LIMIT / _REGISTRY_TTL_S | gact/mcp_apps.py:59-60 | - | mcp-app registry size 64 / TTL 3600s, raw
HARDCODED | _BLUEPRINT_FILE_LIMIT / _BLUEPRINT_TEXT_FILE_LIMIT_BYTES | gact/agent_blueprint_files.py:60-61 | - | blueprint file count 5000 / text-file 2MiB caps, raw
HARDCODED | MAX_OBSERVE_LIMIT / DEFAULT_OBSERVE_LIMIT / OBSERVE_EXCERPT_MAX_CHARS / OBSERVE_MATCH_MAX_CHARS | gact/agents/observe_runtime.py:40-48 | - | observe-tool limits (200/40/600/4000), raw
HARDCODED | _PROCESSOR_WRITE_TIMEOUT_FLOOR_S / _PROCESSOR_MIN_UPLOAD_BYTES_PER_S | gact/resource_processing.py:38-39 | - | processor write-timeout floor 60s / min upload rate 1MiB/s, raw (other processor timeouts ARE resolved)
HARDCODED | _TMP_ORPHAN_GRACE_SECONDS | gact/artifacts/cas_gc.py:59 | - | CAS temp-orphan GC grace 3600s, raw
HARDCODED | _MAX_NOTIFY_BLOCKS | gact/enrichment.py:633 | - | notify-block cap 8, raw
HARDCODED | _MAX_LIVE_TOOL_OUTPUT_CHARS | gact/tool_progress.py:10 | - | live tool-output cap 128KiB, raw
HARDCODED | _REASONING_HEARTBEAT_S | gact/streaming.py:433 | - | reasoning SSE heartbeat 1.0s, raw
HARDCODED | _MAX_WORKFLOW_SCHEMA_LEDGER_ENTRIES | gact/streaming.py:210 | - | workflow-schema ledger cap 64, raw
HARDCODED | DEFAULT_MAX_CONCURRENT_AGENT_TASKS | gact/turn_spawn_executor.py:16 | - | fallback default 3 for agent_tasks.max_concurrent; the resolve default at line 28 uses it but the module const is also referenced raw (used 4x)
HARDCODED | DEFAULT_TURN_TIMEOUT_S | providers/codex_stream.py:64 | - | codex turn timeout 180s, raw function default (limits.codex_sdk_progress_timeout_s IS resolved but this turn-timeout is separate)
HARDCODED | DEFAULT_MCP_TIMEOUT_S | providers/handshake/mcp.py:33 | - | handshake MCP probe timeout 20s, raw
HARDCODED | DEFAULT_TTL_S (handshake cache) | providers/handshake/cache.py:16 | - | handshake result cache TTL 30s, raw
HARDCODED | DEFAULT_TTL_S (models_dev) / _FETCH_TIMEOUT_S | providers/handshake/sources/models_dev.py:40,43 | - | models.dev catalog TTL 24h / fetch timeout 6s, raw
HARDCODED | REFRESH_PER_PROVIDER_DEADLINE_S | providers/model_discovery/refresh.py:30 | - | model-discovery per-provider deadline 90s, raw
HARDCODED | _CODEX_PROBE_TIMEOUT_S / _ACCOUNT_PROBE_TIMEOUT_S / _VERSION_PROBE_TIMEOUT_S | runtime/sandbox_codex.py:367,369,83 | - | codex subprocess probe timeouts (60s/10s/5s), raw
HARDCODED | _CONNECT_READ_TIMEOUT_S / _MAX_CHILD_CHANNELS | runtime/net_chokepoint.py:69,75 | - | net chokepoint connect/read timeout 30s / child-channel cap 128, raw
HARDCODED | _DEFAULT_RUNAWAY_S / _POLL_INTERVAL_S | tools/launcher_cache_lock.py:58-59 | - | launcher-lock runaway 600s / poll 1s (lock_timeout IS resolved; these raw)
HARDCODED | _DEFAULT_HEAL_TICK_S / _POLL_INTERVAL_S | tools/mcp_discovery.py:59-60 | - | discovery heal-tick 20s / poll 0.1s; heal_interval IS resolved, these two module fallbacks/poll raw
HARDCODED | REPEATED_TRANSIENT_FAILURE_LIMIT / SYNC_TOOL_RESULT_GRACE_SECONDS | tools/execution.py:342-343 & tools/mcp_executor.py:111-112 | - | transient-failure retry limit 2 / sync-result grace 1s, raw (duplicated in two files)
HARDCODED | _REPAIR_SCAN_LIMIT / _REPAIR_DEADLINE_S | tools/execution.py:1000-1001 | - | JSON-repair scan cap 20000 chars / deadline 2s, raw
HARDCODED | DEFAULT_LEASE_SECONDS | tools/mcp_task_records.py:86 | - | mcp task-record lease 300s, raw
HARDCODED | CONSOLE_SSE_HEALTHZ_TIMEOUT_SECONDS / CONSOLE_SSE_READ_TIMEOUT_SECONDS / CONSOLE_SSE_MAX_LINE_BYTES / CONSOLE_SSE_MAX_EVENT_BYTES / CONSOLE_SSE_MAX_ATTEMPTS | tools/relay_console_stream.py:90-112 | - | relay console SSE tunables (3s/30s/4MiB/4MiB/2), raw
HARDCODED | RELAY_ARTIFACT_CONTENT_LIMIT_BYTES | tools/relay_artifact_fetch.py:55 | - | relay artifact content cap 16MiB, raw (fetch_max_bytes IS resolved; this separate cap raw)
HARDCODED | RELAY_POLL_INTERVAL_MS | tools/relay_contract.py:13 | - | relay poll interval 1000ms, raw
HARDCODED | RELAY_LOG_PULL_HARD_CAP_BYTES | tools/relay_console.py:96 | - | relay log-pull hard cap 1MiB, raw (pull_limit IS resolved; hard cap raw)
HARDCODED | _JARVIS/_REMOTE_MESSAGE_LIMIT | tools/jarvis_result_contract.py:46 | - | remote message truncation 4000 chars, raw
HARDCODED | _ACTIVITY_CHECK_INTERVAL_S / _WAIT_SURFACE_MIN_INTERVAL_S | tools/mcp_wait_ladder.py:253,352 | - | mcp wait-ladder activity/surface intervals 1s, raw
HARDCODED | _CANCELLATION_POLL_SECONDS / _MCP_WIRE_CANCELLATION_SETTLE_SECONDS | tools/foreground_cancellation.py:14-15 | - | cancellation poll 0.05s / wire settle 1s, raw
HARDCODED | _MAX_REPORTED_SURFACE_IDS | gact/a2ui_tools.py:22 | - | reported surface-id cap 32, raw
HARDCODED | _RESOLVED_HISTORY_LIMIT | gact/user_question_ledger.py:36 | - | resolved-question history limit 100, raw
HARDCODED | _PLAN_STEP_NAME_MAX / _PLAN_SLUG_MAX_LEN | gact/planning.py:684 & gact/plan_mode.py:77 | - | plan step-name 120 / slug 60 char caps, raw
HARDCODED | _DEFAULT_FULL_INTERVAL / _SMALL_FULL_INTERVAL | gact/planning.py:48,51 | - | full-plan refresh intervals 10/20, raw tunable cadence

## TOTALS
OK_REFLECTED: 279
OK_UNREFLECTED: 5
ENV_SANCTIONED: 24
ENV_BARE: 12
HARDCODED: 60
</content>
</invoke>
