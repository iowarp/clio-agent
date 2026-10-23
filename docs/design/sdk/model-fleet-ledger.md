# Model fleet: ledger

Decisions, Phase 0 checks and acceptance evidence for [model-fleet.md](model-fleet.md). Decision ids use the prefix `M-D`; acceptance rows are `M1` to `M12` from the design.

## Phase 0: endpoint checks

| Account kind | Checked | How | Finding |
| --- | --- | --- | --- |
| Claude subscription (`claude_code`) | 2026-10-23, live | One Agent SDK query (claude-agent-sdk 0.2.159, Claude Code 2.1.309) | Every call emits a `rate_limit_event`. Its raw `rate_limit_info` carries `status`, `resetsAt`, `rateLimitType`, overage fields and `unifiedWindows: {five_hour: {utilization, resetsAt}, seven_day: {utilization, resetsAt}}`. Passive capture therefore gives both windows with reset times at no extra cost. |
| Claude subscription, polling | 2026-10-23 | `claude -p /usage --output-format json` | Headless `/usage` reports the session's own cost and tokens only, not plan windows. A cloud session's Claude login is host-managed, so no OAuth token exists for a usage endpoint. Polling is not shipped (M-D2). |
| ChatGPT subscription (`codex`) | 2026-10-23, from source | `openai/codex` `codex-rs/codex-api/src/rate_limits.rs` on `main` | Headers `x-codex-{primary,secondary}-{used-percent,window-minutes,reset-at}` and `x-codex-credits-{has-credits,unlimited,balance}`; SSE event `codex.rate_limits` with `rate_limits.{primary,secondary}.{used_percent,window_minutes,reset_at}` and `credits`. No Codex login exists in the cloud session; the adapter is tested with recorded payloads in this shape. |
| OpenRouter | 2026-10-23, from docs | `openrouter.ai/docs/api-reference/limits` | `GET /api/v1/key` returns `data.{limit, limit_remaining, limit_reset, usage, usage_daily, usage_weekly, usage_monthly, is_free_tier}`; `limit_remaining` is null for an unlimited key. No key in the cloud session; tested with recorded payloads. |
| Anthropic API key | 2026-10-23, from docs | Anthropic rate-limit docs | `anthropic-ratelimit-{requests,tokens,input-tokens,output-tokens}-{limit,remaining,reset}` response headers. No balance endpoint; spend comes from the call ledger. |
| OpenAI API key | 2026-10-23, from docs | OpenAI rate-limit docs | `x-ratelimit-{limit,remaining,reset}-{requests,tokens}` response headers. No balance endpoint; spend comes from the call ledger. |
| Local and site endpoints | n/a | | `unmetered`, with tokens served from the call ledger. |

## Decisions

| ID | Decision | Why |
| --- | --- | --- |
| M-D1 | The design doc is the contract; its phases run as Phase 6 of the megachange plan. RLM leaves (M11) wait for the `rlm` flow, which the plan keeps out of scope. | The plan's scope rules |
| M-D2 | No polling adapter for the Claude subscription. Passive capture from `unifiedWindows` is the source, and an account with no recent call shows its last snapshot with its age. | Phase 0 found no usage endpoint reachable with CLIO's own credential, and passive capture already carries both windows |
| M-D3 | Usage snapshots live in an in-memory board (`USAGE_BOARD`), one per account, not a new store. After a restart an account shows "no reading yet" until its next call or read. | RULE 4: no fifth store; every value is a reading of a live window that the next call refreshes |
| M-D4 | OpenRouter's balance is read on demand when the fleet is read, at most once per `fleet.usage_poll_seconds` (300 s). No background poller. | Only the Usage view and `usage()` need it; a poller would spend calls nobody reads |
| M-D5 | A requirement on a fact the fleet does not report (a context size the provider never states) is recorded as `unverified` in `model_choice.reasons`, not refused. | Refusing on an unknown fact would block every local model; recording it keeps the decision visible |
| M-D6 | Spend is the sum of `Session.usage_by_model`, rolled up as each turn finishes. Sessions from before the rollup count under their pinned model and are listed as `estimated_sessions`. | Replaying transcripts to compute spend took the server to 2.3 GB RSS; the rollup is the session's own record |
| M-D7 | The spending guard covers models CLIO chooses for delegated work on an account the person has not allowed; it asks once per conversation (consent stored on the root session). A model the person picked, an inherited model and the caller's own account never ask. | The person already chose those; the guard is for spend they did not see |
| M-D8 | When the person's own model is exhausted, the turn's error card offers the fleet's alternatives (`switch_model`) and retries the turn on the one picked; nothing switches on its own. When CLIO chose the model, the agent gets `model_exhausted:` with the reset time and alternatives and decides. | Superseding principle 1: the model decides its own routing; the person decides theirs |
| M-D9 | The model picker keeps every model and shows a notice naming the models that fit the agent's requirements; sending on a model that misses one is refused with `model_requirement_unmet` (422). | Hiding rows made a missing model look disconnected; the refusal carries the reason |
| M-D10 | A run's model shares count tokens read and written, not cache reads. | Cache reads dominated and made a short synthesizer step look like most of the run |
| M-D11 | The line telling the agent to name a model for a subtask lives in the `fleet` plugin's `fleet.models` fragment, not a `delegation.parent` fragment. | No `delegation.parent` fragment exists under plugins; the fleet plugin owns the tools the line refers to, and `core` requires it |
| M-D12 | Rate-limit headers of Anthropic and OpenAI API keys are not captured yet; those accounts show spend from the session rollups. | DSPy runs these calls on either its lm15 or its LiteLLM backend, and successful lm15 responses do not expose headers, so capture needs a recording transport passed through `RouterConfig.transport` and a LiteLLM hook. Neither can be exercised without a key in the cloud session. Follow-up |
| M-D13 | `summarizer.provider`, `summarizer.api_base`, `summarizer.credential_ref`, `summarizer.transport`, `summarizer.model` and `summarizer.max_tokens` are deleted. | Nothing read them: the goal judge and the permission reviewer pass their own model, and a role on another account is chosen in `fleet.judges` as an account and model |
| M-D14 | A `Prefers(effort=...)` sets the child's thinking level after the message's own level and before the parent's. A preference naming only an effort applies to whichever model runs. | The design maps `effort` onto the thinking levels; a level the model cannot carry shows as unsupported in the turn's provenance |

## Acceptance

Status `unit` means the behaviour is covered by tests but the cloud session has no account that can produce it live.

| ID | Status | Evidence |
| --- | --- | --- |
| M1 | partial | Claude live: the Usage view (`fleet-2-usage-settings`, `fleet-3-usage-remaining` at 1440x900; `fleetp-*` at 390x844, read) shows the five-hour and seven-day windows with resets from `unifiedWindows`. Codex and OpenRouter read from recorded payloads in the shapes Phase 0 found (`tests/test_gact/test_fleet.py::test_codex_headers_carry_both_windows_and_credits`, `::test_openrouter_key_response_is_a_balance_and_a_key_window`). No Codex login, OpenRouter key or vLLM endpoint exists in the cloud session. |
| M2 | partial | The footer hover during a Claude session shows the live windows (`fleet-4-footer-hover`, read); the phone layout has no footer readout (`fleetp-4-footer-no-readout`). `usage()` reads the same `USAGE_BOARD` snapshot; the two were not compared side by side in one session. |
| M3 | passed | Live: the agent called `list_models`, then spawned a child on `claude_code/claude-haiku-4-5-20251001`. Child `sess_fb2789b01934…` (parent `sess_b7b5aa87d4ed`) ran on it with `model_choice.source: explicit`, its turn on the session-pinned model. |
| M4 | unit | `tests/test_gact/test_fleet_selection.py::test_an_unmet_preference_steps_down_the_order_and_records_why`, `::test_a_preference_is_used_and_names_its_source`. Not run live: every connected model in the cloud session is a Claude model. |
| M5 | unit | `test_fleet_selection.py::test_an_explicit_model_without_a_required_capability_is_refused_with_alternatives`, `::test_an_inherited_model_that_cannot_run_the_child_is_refused`; the person side is `test_fleet_session_check.py::test_a_model_without_what_the_agent_needs_is_refused_with_the_models_that_fit`. Not run live: every connected Claude model has vision. |
| M6 | passed | Live: a workflow with steps on `claude-sonnet-5`, `claude-haiku-5-5` and `claude-haiku-4-5-20251001` under `sess_2f296ddf…` (parent `sess_a68c123adbbd`). Each step's child records `source: explicit` and ran on its model. The railroad summary and the step labels name the models (`m6b-2-workflow-summary`, `m6b-3-workflow-step`, `m6b-4-usage`, read). Export carries `Step.model`, and each child session's `model_choice` travels in its exported session metadata. |
| M7 | unit | `tests/test_gact/test_fleet_guard.py::test_a_metered_account_asks_once_per_conversation`, `::test_a_denied_account_is_a_typed_refusal`, `::test_allowed_accounts_inherited_models_and_the_callers_own_account_never_ask`. Not run live: no metered account in the cloud session. |
| M8 | unit | `test_fleet_guard.py::test_model_exhausted_says_who_chose_and_what_else_can_run`, CLIO-chosen branch: the `model_exhausted:` message with `resets_at` and alternatives. Codex plan limits are read by error code (`tests/test_providers/test_codex_errors.py`). An exhausted window cannot be produced on demand. |
| M9 | unit | The same test, person-chosen branch: a `switch_model` recovery action and no switch. The error card (`TurnModelExhausted`) retries the turn on the picked model; it was not exercised against a real exhausted window. |
| M10 | unit | `test_fleet.py::test_a_failed_read_keeps_the_last_good_snapshot_and_says_why`, `::test_openrouter_is_read_with_its_own_key_and_a_failure_keeps_the_value`. The Usage view dims a snapshot that has `read_failure` and shows its age (`fleet-usage.tsx`); not captured with the network down. |
| M11 | not started (waits for `rlm`, M-D1) | |
| M12 | passed | Live: council child `sess_bd0a308c…` (parent `sess_4a18f5ecb8e2`) with members on `claude-haiku-5-5` and `claude-haiku-4-5-20251001`, each `source: explicit`. The railroad summary names both models with their token shares and the Claude five-hour window; the hover on each member names its model and source (`fleet-5-council-summary`, `fleet-6-council-member`, `fleetp-5-*`, `fleetp-6-*`, read). |

## Work view

A child's background-task row and its card show the model it runs on with the provider's logo; hovering names why. Live: child of `sess_c383f1e3b37c` on `claude-haiku-4-5-20251001` shows "claude-haiku-4-5", and the hover reads "Runs on claude_code/claude-haiku-4-5-20251001: named for this call." (`sm-5-row`, `sm-6-hover` at 1440x900; `smp-5-row` at 390x844, read). Server: `tests/test_gact/test_fleet.py::test_a_childs_task_row_and_live_event_carry_its_model_choice`; UI: `subagent-card.test.tsx`.
