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
- [ ] Stale model refs still trigger `501 session model overrides` after changing model/provider and sending the next message. Previous backend-only clearing did not cover the full path; trace whether the TUI sends a per-message model override or keeps stale local session state, then remove/clear that path.
- [ ] Provider/model changes must be applied at Save time, not at next Send time. Saving should load/swap the selected model as soon as possible, clear stale session refs immediately after the active turn is safe to mutate, update TUI local session state, and leave the next user message with no model-loading or stale-override cleanup work.
- [x] Make ALCF Refresh token actually force the backend auth flow. The ready-token button now sends `force=true`; Authenticate still uses the normal first-login/check path.
- [ ] ALCF auth readiness is still too optimistic. The UI/backend must not treat "token file exists" or a cached status as "usable token"; readiness should be based on successfully minting/refreshing an access token and, ideally, validating a lightweight authenticated gateway request. Expired or unrefreshable tokens must surface as auth_required/unavailable with an explicit re-auth action, not "Globus token ready".
- [ ] ALCF auth refresh is currently attempted inside the noninteractive backend request thread. Globus then hits EOF waiting for an authorization code. TUI auth must launch a visible interactive terminal/login process or return a clear manual command, not run the code prompt hidden inside the server.
- [ ] Settings Agent tab still has wire-shape drift: CLIO returns `AgentDef.parameters` as an object, while the TUI shared type expects `[]AgentParameter`. The TUI must tolerate both shapes or CLIO must emit the shared contract shape.
- [ ] Model configuration keyboard navigation is incomplete. Once focus is on Temperature/Max output/Context length/Thinking budget, Up/Down should move between rows in that panel and Left/Right should adjust the focused value.
- [ ] Header/top bar semantics are confusing on load. It can show both a stale per-session model (`model: claude-opus-4-7`) and the active global CLIO model (`model: Meta-Llama-3.1-8B-Instruct`), plus opaque labels like `ws: default`, `agent: default`, and raw backend URL `http://127.0.0.1:...`. Header should show the deployment identity/name (for example `myclio (clio)`), a human workspace label, one authoritative model label, and a meaningful agent/expert mode label.
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
