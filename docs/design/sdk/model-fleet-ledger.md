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

## Acceptance

| ID | Status | Evidence |
| --- | --- | --- |
| M1 | not started | |
| M2 | not started | |
| M3 | not started | |
| M4 | not started | |
| M5 | not started | |
| M6 | not started | |
| M7 | not started | |
| M8 | not started | |
| M9 | not started | |
| M10 | not started | |
| M11 | not started (waits for `rlm`, M-D1) | |
| M12 | not started | |
