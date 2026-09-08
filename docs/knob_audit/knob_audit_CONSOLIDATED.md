# Config-knob audit — consolidated (3 independent auditors: opus/sonnet/haiku, merged + disputes verified)

Scope: src/clio_agent only. Law: conf.py four-layer ladder; violations = bare env reads
outside conf.py's documented exemption tiers (bootstrap / secret / auth-probe), and
operator tunables with no conf key.

## ENV_BARE — consensus violations (should route through conf.resolve)
ENV_BARE | CLIO_ONLYOFFICE_URL | gact/documents/editors.py | 3/3 auditors — whole document-editor integration outside conf
ENV_BARE | CLIO_COLLABORA_URL | gact/documents/editors.py | 3/3
ENV_BARE | CLIO_GACT_PUBLIC_URL | gact/documents/editors.py | 3/3
ENV_BARE | CLIO_ONLYOFFICE_JWT_SECRET | gact/documents/editors.py | 3/3 — secret-tier candidate: either conf-exempt-document it or CLIO_CRED_*
ENV_BARE | CLIO_DOCUMENT_TYPST_FONT | gact/documents/renditions.py:145 | 3/3
ENV_BARE | CLIO_ARC_STORE (duplicate read) | arc/init_degradation.py:112 | 2/3 — bypasses its own conf.resolve counterpart
ENV_BARE | CLIO_RUNTIME_STATE_DIR | arc/clio_core_config.py:79 | 2/3 — no conf key at all; not on bootstrap exemption list
ENV_BARE | CHI_SERVER_CONF | arc/clio_core_liveness.py:145 | 2/3 — legacy alias; possible typo lineage (CLIO_SERVER_CONF?)
ENV_BARE | LM_STUDIO_API_TOKEN, LM_API_TOKEN | gact/providers/lmstudio.py:44-45 | 3/3 — secrets, but NOT on conf.py's documented secret-tier list; add them there or migrate

## ENV — borderline / deliberate (document rather than change)
ENV_HYBRID | CLIO_TRANSIENT_PROVIDER_RETRY_DELAYS | agent.py:867+880 | conf.resolve + supplementary bare read to distinguish set-but-empty; deliberate, documented in place
ENV_HYBRID | CLIO_PROVENANCE_PROVIDERS / CLIO_SEMANTIC_TRACE_BACKEND | provenance_config.py:34,40 | hand-rolled file_value+env precedence to detect explicit-legacy (#1247) — deliberate, but the pattern predates conf and deserves a conf-native mechanism
ENV_SANCTIONED-ish | CODEX_HOME, APPDATA/LOCALAPPDATA/XDG_DATA_HOME, Windows build-info vars | various | third-party SDK homes + platform bootstrap; fine, but conf.py's exemption list doesn't mention them — extend the documented list

## OK_UNREFLECTED — conf.resolve correct, missing from config.defaults.yaml
OK_UNREFLECTED | tools.mcp.elicitation.agent_audience.answer_mode | gact/agent_elicitation.py | OURS — FIXED in this pass
OK_UNREFLECTED | tools.mcp.elicitation.agent_audience.default_unhinted | gact/agent_elicitation.py | OURS — FIXED in this pass
OK_UNREFLECTED | provenance.agentic.flowcept.settings_path (FLOWCEPT_SETTINGS_PATH) | gact/provenance/factory.py:130 | 2/3
OK_UNREFLECTED | scheduler.tz (TZ fallback) | gact/scheduler.py:236 | 2/3 — scheduler.timezone primary IS documented

## HARDCODED — consensus operator-tunable literals (no conf key)
HARDCODED | autonomous_loop.py WAKEUP_MIN_S/WAKEUP_MAX_S/DEFAULT_INTERVAL_S/MAX_ITERS/MAX_WALLCLOCK_S | 3/3 — loop pacing entirely unconfigurable
HARDCODED | DEFAULT_MAX_GOAL_ITERS=25 | gact/goal.py | 2/3
HARDCODED | MAX_SPAWN_DEPTH=8 | gact/session_descendants.py | 2/3 — documented as a backstop, still un-tunable
HARDCODED | _RUNTIME_START_TIMEOUT_S=30 | arc/storage.py | 2/3 — bites on slow shared filesystems (#1319 class)
HARDCODED | _FILE_TIER_FREE_SPACE_RESERVE_BYTES (1GiB) | arc/clio_core_file_capacity.py | 2/3
HARDCODED | scheduler backoff constants | gact/scheduler.py | 2/3
HARDCODED | mcp_apps listing/cache constants | gact/mcp_apps.py | 2/3
HARDCODED | shell/relay console stream caps | tools/relay_console_stream.py, tools/execution.py | 2/3
HARDCODED | handshake probe timeouts | providers/handshake/* | 2/3
HARDCODED | elicitation DEFAULT_ELICITATION_TIMEOUT_S=600 | gact/elicitation_bridge.py | 1/3 but load-bearing (== MCP backstop; changing one without the other breaks headless decline)
(+ ~30 single-auditor candidates of decreasing signal — full lists in /tmp/knob_audit_{A,B,C}.md)

## Classifier false-positives removed in merge
- CLIO_LM_GUIDED_OUTPUT: proper conf.resolve (lm.guided_output), reflected — C misread.
- C counted "documented-unset" defaults entries as UNREFLECTED; A/B (and the drift test) treat them as reflected. A/B semantics adopted.

## Totals after merge
- conf.resolve keys: ~284 (A's enumeration, most complete), ≥279 reflected
- True ENV_BARE violations: 9 distinct knob-groups (11 vars)
- Deliberate hybrids to document: 3 groups
- Consensus HARDCODED tunables: ~10 groups (+ long tail)
