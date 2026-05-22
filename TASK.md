# CLIO/GACT Provider Selector Polish Tasks

## Open Issues

- [x] Status text overflows or truncates despite available space. Long transport errors and provider status messages now wrap across multiple lines in the Selected panel.
- [x] Long API bases truncate instead of wrapping cleanly. ALCF URLs and custom local endpoints now hard-wrap without ellipses.
- [x] Remove all remaining "ALCF local vLLM (compute-node)" / compute-node-specific local-vLLM wording. The provider is simply "vLLM (localhost)" in current source and verified screenshots.
- [x] Make LM Studio reachable on initial provider check, without requiring Ctrl+R after the modal opens.
- [x] Verify Ollama live detection against the running local service and surface the real reason if it is not usable. Current machine: Ollama is reachable but has no installed models.
- [x] Fix Codex and Claude Code model catalogs. Static candidate catalogs show reliably without a live `/models` endpoint.
- [x] Confirm Claude Code/Codex model selection semantics and do not present lack of scriptable live model discovery as lack of model selection.
- [x] Remove jarring black row backgrounds inside the modal. Text rows now use the modal background consistently.
- [x] Add model filtering/search inside the Model section.
- [x] Sort model catalogs alphabetically before rendering/selecting.
- [x] Add a model-details surface when providers expose useful metadata, and document when context/window size is not discoverable.
- [x] Treat LM Studio context length as a load-time setting, not a completion token limit. The backend now reads LM Studio native model metadata and applies requested context length through LM Studio's native load API before swapping CLIO's global LM.
- [x] Keep provider-specific model knobs honest. LM Studio shows context length; Codex/Claude Code hide unsupported numeric tuning; temperature/max output remain request-time settings.
- [x] Stop stale per-session ModelRefs from breaking global provider swaps. New/duplicated TUI sessions no longer carry default Anthropic model refs, and backend provider save clears stale session model refs.
- [x] Make ALCF auth action wording stateful. The selector shows Authenticate when no token is ready and Refresh token when Globus auth is already ready.
- [x] Fix Settings Agent tab decode failure when `/v1/agents` returns `default_model` as a string. The TUI wire type now accepts both legacy string model ids and structured `ModelRef` objects.
- [x] Fix incongruent context display. When a provider does not report model context but the user has configured a context length, the TUI now reports the configured context instead of saying "not reported".
- [x] Stale model refs still trigger `501 session model overrides` after changing model/provider and sending the next message. The backend heals stale session refs when a global LM is configured, the TUI now clears cached session ModelRefs after Save, and a live LM Studio/Qwopus probe returned `POST /messages` 200 rather than 501 after provider save.
- [x] Provider/model changes must be applied at Save time, not at next Send time. Saving now loads/reuses the selected LM during `PUT /v1/providers/lm`, clears backend and TUI session refs immediately, mirrors global provider info locally, and queues a session refresh before the next send.
- [x] LM Studio provider save can fail while the requested model is already loaded. Live repro: `/api/v1/models` reported five loaded `qwopus3.5-9b-v3` instances at 32768 context, but `PUT /v1/providers/lm` called `/api/v1/models/load` anyway and surfaced `model_load_failed`. CLIO now reuses an already-loaded LM Studio instance when its context matches the requested context, and only calls the load endpoint when a load or context change is actually required.
- [x] Real LM Studio/Qwopus turn can remain `running` after a successful provider save and accepted `POST /messages`. Live repro after the stale-model fixes: `PUT /v1/providers/lm` returned 200, stale session model cleared, `POST /messages` returned 200, but after 45 seconds no assistant message existed and the session was still `running`.
  - Fixed by adding `CLIO_GACT_TURN_TIMEOUT_S` watchdog around GACT turn execution. On timeout, CLIO now emits/persists an assistant `provider_timeout` error, sets the session to `error`, preserves partial streamed text if any, and labels executor cancellation as `best_effort` / `executor_work_may_continue=true` instead of hiding the failure or leaving the turn running forever. The existing no-agent path still returns structured `503 config_error` before the watchdog.
  - Evidence: live LM Studio/Qwopus probe with `CLIO_GACT_TURN_TIMEOUT_S=5` returned provider save 200 and post 200, then persisted an assistant `error_info.error=provider_timeout`, `stop_reason=error`, and session `status=error` instead of remaining `running`.
- [x] Make ALCF Refresh token actually force the backend auth flow. The ready-token button now sends `force=true`; Authenticate still uses the normal first-login/check path.
- [ ] ALCF auth readiness is still too optimistic. The UI/backend must not treat "token file exists" or a cached status as "usable token"; readiness should be based on successfully minting/refreshing an access token and, ideally, validating a lightweight authenticated gateway request. Expired or unrefreshable tokens must surface as auth_required/unavailable with an explicit re-auth action, not "Globus token ready".
- [ ] ALCF auth refresh is currently attempted inside the noninteractive backend request thread. Globus then hits EOF waiting for an authorization code. TUI auth must launch a visible interactive terminal/login process or return a clear manual command, not run the code prompt hidden inside the server.
- [x] Settings Agent tab still has wire-shape drift: CLIO returns `AgentDef.parameters` as an object, while the TUI shared type expects `[]AgentParameter`. Current `gact-tui develop` now tolerates both shapes via `AgentDef.UnmarshalJSON`; focused evidence: `go test .\emulator\pkg\gact -run "TestAgentDef" -count=1`.
- [ ] Model configuration keyboard navigation is incomplete. Once focus is on Temperature/Max output/Context length/Thinking budget, Up/Down should move between rows in that panel and Left/Right should adjust the focused value.
- [x] Header/top bar semantics are confusing on load. It can show both a stale per-session model (`model: claude-opus-4-7`) and the active global CLIO model (`model: Meta-Llama-3.1-8B-Instruct`), plus opaque labels like `ws: default`, `agent: default`, and raw backend URL `http://127.0.0.1:...`. Header now accepts a deployment label, spells out `workspace:`, suppresses default/no-op agent labels, and treats the configured global CLIO LM as authoritative over stale per-session model refs.
  - Progress target: change the TUI header contract so connected deployments can pass a display label (`name (type)`), workspace is spelled out, default/no-op agent labels are suppressed, and the global configured LM is treated as authoritative over stale per-session model refs. Add focused render tests and screenshot evidence before marking done.
  - Evidence: focused Go render tests passed, and VHS screenshot `screenshots/header-semantics.png` shows `myclio (clio)` with `workspace: default` instead of the raw URL / `ws:` label.
- [x] Windows VHS helper left `$GACT_BACKEND` literal after rewriting tapes from bash to `cmd`, causing screenshots to connect to `"$GACT_BACKEND/v1/capabilities"`. The helper now substitutes `$GACT_BACKEND` and `$GACT_BACKEND_LABEL` from the process environment before running VHS.
- [ ] Tool-call observability is ambiguous in conversation. The assistant may say it called tools, but the TUI does not clearly show tool call events, arguments, results, or failures. Need to verify whether tools are actually executing and hidden, or not executing despite textual claims; then render tool-call evidence/results in the conversation or explicitly label unsupported/unobserved claims.
- [ ] TUI lacks an internationalization/localization architecture. User-facing strings appear hardcoded throughout the TUI, which makes future support for other languages and non-Roman alphabets difficult. Need an early i18n plan: string IDs/catalog files, locale selection, fallback semantics, Unicode width/wrapping tests, and visual verification for non-Latin scripts.
## Verification Required

- [x] Focused Go TUI tests.
- [x] Focused Python provider tests.
- [x] Focused post-message stale-model regression tests.
- [x] Focused GACT wire decode test for string/object `default_model`.
- [x] Live LM Studio probe.
- [x] Live LM Studio native context metadata probe: Qwopus reports 262144 context tokens on this machine.
- [x] Live Ollama probe.
- [x] VHS screenshots for default provider selector, ALCF, Codex, Claude Code, and short-viewport views.
- [ ] Restart or redeploy the user's CLIO backend/TUI session so labels come from current code instead of the old process on port 61671.
