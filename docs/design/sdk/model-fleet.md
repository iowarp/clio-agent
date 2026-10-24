# CLIO model fleet and usage

Oct 22, 2026 · @Jaime

## Summary

Choosing the model becomes a first-class part of CLIO 1.0, on the same footing as plugins and flows. Today a whole session tree runs on one model: the person picks it, and every subagent, flow call and workflow step inherits it unchanged. After this change, every place that starts model work can name a model, CLIO itself can choose one, and both the person and the agent can see what each connected account has left.

The design has four parts that share one data source:

1. **The fleet.** One registry of connected accounts and the models each serves, joined with the capability records CLIO already keeps (context, tools, vision, price).
2. **Usage.** One adapter per provider reports windows, percent used, reset times and remaining balance. Claude and Codex subscriptions, OpenRouter credit, API-key accounts and local endpoints all report in the same shape.
3. **Selection.** `Prefers` and `Requires` finally drive model choice, and spawns, `ctx.call`, workflow steps and RLM leaves accept an explicit `model`. Every choice is checked against the fleet and recorded; a preference that cannot be met is reported, never silently replaced.
4. **Surfaces.** Agent tools (`list_models`, `usage`) and the UI from [issue #1802](https://github.com/iowarp/clio-agent/issues/1802): a Usage view of every account, and a hover readout under the chat box for the account in use. The widget idea comes from [quotabubble](https://github.com/izzet/quotabubble).

## Current state

Checked against `claude/sdk-plugins-flows` at `b4cf79b8`. The pieces exist, but nothing connects declarations to the model a session actually uses.

| Piece | State at the base | Where |
| --- | --- | --- |
| Session model | A dict `{provider_id, model_id}` set at creation from the request or from session defaults | `gact/routes/session_creation.py:49` |
| Child sessions | Copy `parent.model` exactly; spawn tools, `ctx.call` and workflow steps have no model argument | `gact/spawn_context.py:150` |
| Declarations | `Requires` (tool calling, vision, context and output tokens) and `Prefers` (model, provider, effort) on `Agent`, `Subagent` and `Plugin` | `sdk/model.py` |
| Use of declarations | `Requires` is unioned across plugins into the resolution. `Prefers` is recorded and never read when a session is created | `plugins/resolve.py:556`, `gact/agents/resolution.py:205` |
| Providers | 19 presets including `claude_code`, `codex`, `openrouter`, `anthropic`, `openai`, `vllm`, `ollama`, `argonne_sophia`, `argonne_metis`, `amsc_mag` | `providers/catalog.py` |
| Capabilities | Model, Endpoint and Deployment records, every field a `Fact` with source and time; `TokenPricing` for list and endpoint prices | `providers/capabilities/records.py` |
| Secondary roles | One, `summarizer`, configured by file or environment | `lm/secondary.py:15` |
| Agent-facing tools | Only `refresh_provider_models` (the update-models skill). No tool lists accounts, models or usage | `core` plugin `tools/runtime.py` |
| Claude usage | The Agent SDK's rate-limit events carry `utilization`, `resets_at` and the limit type; CLIO reads them only to classify a rejection | `providers/claude_code_plan_limit.py` |
| Codex usage | A usage limit is detected by matching error text | `providers/codex/direct_engine.py:399`, `codex/errors.py` |
| Recovery | Plan-limit and safety errors suggest `switch_model`, but nothing performs it | `providers/claude_code_plan_limit.py:192` |

The Codex text match breaks the existing rule against keyword-matching prose where a structured field exists, and is replaced in Phase 2.

## Design: the fleet

The fleet is a read model over what CLIO already stores, not a new store. It joins three things: the person's connected accounts, the models each account can serve, and the capability records for each model and endpoint.

**Account.** One configured provider instance with its credential: a Claude subscription through the Agent SDK, a ChatGPT subscription through Codex OAuth, an OpenRouter key, an Anthropic API key, a vLLM router on a homelab, an Argonne endpoint. An account has an id, a provider preset, a billing kind (`subscription`, `credit`, `metered`, `free`) and a health state from the existing provider probe.

**Fleet entry.** One `(account, model)` pair, carrying:

- the combined capabilities from `providers/capabilities/combine.py`, so `Requires` can be checked without a new code path;
- price per million input and output tokens from `TokenPricing`, with its source, and zero for free endpoints;
- the latest usage snapshot for the account (next section);
- an `available` flag that is false when the account is unhealthy, exhausted or the model is not served.

**Freshness.** Model lists come from the existing discovery and `refresh_provider_models`. Usage is refreshed by its adapters. Every fleet field keeps its `Fact` provenance, so the UI and the agent see where a value came from and how old it is.

**One owner.** `GET /v1/fleet` serves the UI, the agent tools read the same function in-process, and the selection logic uses the same function. There is no second model list anywhere, and the hard-coded fallback lists that remain are deleted.

## Design: usage

**One protocol.** Each provider preset may ship a usage adapter, following quotabubble's `Provider` protocol shape. It returns a `UsageSnapshot`: a list of windows (name, percent used, resets at), an optional balance (amount, currency, limit), the time observed, and the source. A provider with no notion of quota returns `unmetered` rather than nothing, so the UI never shows a blank.

**Two ways to learn usage.** Passive capture reads what calls already return, at no extra cost. Polling asks the provider when the account is idle, every 5 minutes by default. Where both exist, passive capture wins while the account is in use.

| Account kind | Passive | Poll | Verified? |
| --- | --- | --- | --- |
| Claude subscription (`claude_code`) | SDK rate-limit events: `utilization`, `resets_at`, limit type | Subscription usage endpoint behind the same OAuth login | Passive is in the code today; the poll endpoint is checked in Phase 0 |
| ChatGPT subscription (`codex`) | Rate-limit fields on Codex responses | Codex usage endpoint behind the OAuth login | Both checked in Phase 0 |
| OpenRouter | Per-call cost in the response | Key and credits endpoints: limit, usage, remaining credit | Endpoints documented by OpenRouter; checked in Phase 0 |
| Anthropic or OpenAI API key | Rate-limit response headers | None for balance | Headers checked in Phase 0; balance shows as spend so far from CLIO's own ledger |
| vLLM, Ollama, llama.cpp, LM Studio, homelab router | Tokens from the call ledger | None | `unmetered`, with tokens served |
| Site endpoints (Argonne, AmSC) | Tokens from the call ledger | Whatever the site exposes, if anything | Per site; `unmetered` until one exists |

**CLIO's own credentials only.** Adapters use the credential the account was connected with. Unlike quotabubble, CLIO never reads another tool's credential files or keychain entries.

**Spend ledger.** The `provider.call_usage` audit rows already record tokens and cost per call. A rollup by account, session and agent gives spend for accounts with no balance endpoint and lets the person see which agent or study spent what.

**Failure.** A failed poll keeps the last good snapshot, dimmed and marked with its age, and records why. It never clears the value.

**Codex limit detection.** The text match in `codex/errors.py` is deleted and replaced by the structured fields the adapter reads, in line with the rule against prose matching.

## Design: selection

**Where a model can be named.** Every place that starts model work takes an optional `model`:

| Place | How |
| --- | --- |
| Root session | The person's picker, as today |
| Spawn tools | `spawn_agent_task(..., model=)` and per item in `spawn_agents_parallel` |
| Flows | `ctx.call(target, input, model=)`, and a class attribute for a flow's own default |
| Workflow steps | A `model` field on a step, editable by editors like any other field |
| Defined subagents | `Subagent(prefers=Prefers(model=..., provider=..., effort=...))` |
| Agents and plugins | `Prefers` and `Requires` on the class, as declared today |
| RLM leaves | `llm(query, context, model=)`, and the `rlm` subagent's own `Prefers` |
| Secondary roles | `summarizer` and any later role name a fleet entry instead of a separate provider config |

**A model reference** is either an exact `account/model` pair or a bare model id, which resolves to an account that serves it, preferring the caller's own account. `effort` maps onto the existing thinking levels.

**Resolution order** when a session or call starts:

1. An explicit `model` on the call.
2. The target's `Prefers` (subagent, then agent, then plugins in resolution order).
3. The caller's model, which is today's behaviour.

The first candidate is checked against the session's effective `Requires` and the fleet's `available` flag. An explicit model that fails the check is a typed refusal (`model_unavailable`, `model_requirement_unmet`) returned to the caller, so the model can pick another. A preference that fails moves to the next step, and the child records why.

**CLIO choosing.** The agent chooses by calling `list_models` and passing a `model`. Nothing picks a model behind its back. The `delegation.parent` fragment gains one line: name a model when a cheaper or larger one fits the subtask, and check `usage` first for long fan-outs.

**Recording.** Every session stores `model_choice`: what was asked, what ran, the source (`explicit`, `prefers`, `inherited`, `person`), and the reason for any step down the order. The Work view shows the model per row as an icon with details on hover, and export and provenance carry it.

**When a window runs out.** A plan-limit or credit rejection already arrives as a typed error. The session's next model call then follows one rule: if the person chose the model, CLIO asks through a Question with the alternatives the fleet offers; if CLIO or a preference chose it, the agent receives `model_exhausted` with the reset time and decides itself. There is never an automatic silent switch.

**Spending guard.** Settings gain a per-account switch, **CLIO may use this account for delegation**, on by default for subscriptions, local and free endpoints and off for metered and credit accounts. Choosing an account that is off raises a Question once per session. This keeps an agent from spending OpenRouter credit the person did not intend to spend.

**Cache.** A child's model is fixed when the child is created, so prompt caching inside a session is not broken by selection. Only the person switches a running root session's model.

## Design: agent tools and UI

**Tools**, in a `fleet` plugin that `core` requires, so every agent has them:

| Tool | Returns | Read-only |
| --- | --- | --- |
| `list_models(filter=None)` | Fleet entries the session may use: account, model, key capabilities, price, `available`, and a one-line usage summary. Filter by requirement, provider or billing kind | Yes |
| `usage(account=None)` | Usage snapshots: windows with percent and reset time, balance, age, spend by this session tree | Yes |
| `refresh_provider_models` | Unchanged; moves from `core` into `fleet` | No |

Results are compact by default, because a fleet with several local models can list dozens of entries. Both tools work from inside the `run_code` kernel, where filtering a long list is cheap.

**Usage view** ([#1802](https://github.com/iowarp/clio-agent/issues/1802)). A page in Settings, and a panel reachable from the chat footer, listing every connected account. Each row shows the provider icon, billing kind, a bar per window with its reset countdown, the balance for credit accounts, spend in this workspace, and health. Rows are grouped subscriptions first, then credit, metered and local. A toggle switches between percent used and percent remaining. A stale value is dimmed with its age on hover.

**Hover readout.** The tokens and cost readout under the chat box gains a hover card for the account the session runs on: its windows, reset times and balance, plus the tokens and cost this session and its children used. When a window passes 80% the readout shows a small marker, with no banner and no toast.

**Model in the Work view.** Each row carries a model icon; hover shows the `model_choice` record. A study or flow run can be read at a glance as "which branches ran on what".

**Usage on the flow railroad.** The railroad of a flow or workflow run carries a brief summary under its title: the models the run used, each with its share of the run's tokens and cost, and the windows of the accounts it ran on (for example `sonnet 61% · haiku 39% · Claude 5h window 42%`). Each box's hover adds the model it ran on and its `model_choice` source. People reading a run see what it cost and where without leaving it; the Usage view stays the place for every account.

**Desktop widget.** A quotabubble-style floating widget is a natural fit for the Tauri desktop app later. It reads `GET /v1/fleet` from any connected agent, including remote ones, so one widget can cover several agents. It is listed here so the endpoint is designed for it; building it is optional for 1.0.

## Open questions

Each has a proposed default so a plan can be written from this document.

| Question | Options | Proposed default |
| --- | --- | --- |
| Can CLIO pick automatically, with no model named | An `auto` policy (cheapest model meeting `Requires`); only the agent choosing through `list_models` | Agent chooses. An `auto` policy hides a cost decision inside the runtime; revisit after 1.0 with evidence from `model_choice` records |
| Ordered preferences | `Prefers.model` as one id; as an ordered list | One id. A list becomes a maintained fallback list in code, which is what the model catalog work removed |
| Spending guard default | All accounts allowed; metered and credit accounts need consent | Subscriptions, local and free allowed; metered and credit need a Question once per session |
| Who owns a cross-account choice in a workflow | The editor that set it; the step's agent | Whoever wrote the step's `model` field; a step's own agent can still pass `model` to its spawns |
| Polling the Claude and Codex usage endpoints | Poll when idle; passive only | Poll when idle, every 5 minutes, only for accounts connected in CLIO, and only if Phase 0 confirms the endpoints are stable to call with our tokens |
| Budget limits | Per session token or dollar budgets on top of usage | Out of scope for this item. Flow limits already carry `budget_tokens`; dollar budgets come later |
| Remote agents | Each agent reports its own fleet; the desktop merges | Each agent serves its own `/v1/fleet`; the UI shows them grouped by agent, with no merge |
| Mid-session switch by the agent | Allow the agent to change its own model; only children get a choice | Only children. The agent forks or spawns onto another model instead, which keeps each transcript on one model |

## Phases and qualification

This item touches session creation and the spawn path, so it runs after the delegation plan lands and before RLM's Phase 3, which uses per-leaf model choice. The fleet and usage phases do not depend on delegation and can start now. Decisions use the prefix `M` in their own ledger.

1. **Phase 0: inventory and checks.** Confirm the Claude and Codex usage endpoints and their fields with real tokens; confirm OpenRouter's key and credits responses and the Anthropic and OpenAI rate-limit headers; list every hard-coded model list left in the code; map every place a child session gets its model.
2. **Phase 1: the fleet.** The read model, `GET /v1/fleet`, accounts with billing kind and health, entries joined with capabilities and price, deletion of the remaining hard-coded lists.
3. **Phase 2: usage.** The adapter protocol and `UsageSnapshot`, adapters for `claude_code`, `codex`, `openrouter`, `anthropic`, `openai` and an `unmetered` adapter for local and site endpoints, passive capture from the existing signals, idle polling, last-good handling, the spend rollup from `provider.call_usage`, and replacement of the Codex text match.
4. **Phase 3: selection.** `model` on spawn tools, `ctx.call`, workflow steps and `llm()`; `Prefers` applied at session creation; the resolution order and its typed refusals; `model_choice` recorded and exported; `model_exhausted` and the Question for person-chosen models; the spending guard; `summarizer` moved onto a fleet reference.
5. **Phase 4: tools and fragment.** The `fleet` plugin with `list_models` and `usage`, `refresh_provider_models` moved into it, and the `delegation.parent` line.
6. **Phase 5: UI.** Usage view, hover readout, model icon and `model_choice` hover in the Work view and railroad, and the railroad's usage summary. Verified with `ui_check.mjs` at 1440 by 900 and 390 by 844.
7. **Phase 6: qualification.**

| ID | Scenario | Passes when |
| --- | --- | --- |
| M1 | Open the Usage view with Claude, Codex, OpenRouter and a local vLLM connected | Every account shows windows or balance with reset times; vLLM shows `unmetered` with tokens served |
| M2 | Hover the chat footer during a Claude session | The card shows the live window values from the SDK events, matching `usage()` |
| M3 | The agent calls `list_models`, then spawns a child on a smaller model | The child runs on that model; `model_choice` says `explicit` |
| M4 | A subagent declares `Prefers(model=...)` for a model that is not connected | The child runs on the parent's model and `model_choice` records the unmet preference |
| M5 | A spawn names a model without vision for a task whose plugins require vision | `model_requirement_unmet` is returned and nothing starts |
| M6 | A workflow with steps on three different models | Each step runs on its model; the railroad shows them; export carries them |
| M7 | A spawn on a metered OpenRouter model with the guard off | One Question; after approval the spawn runs, and a second spawn in the session does not ask again |
| M8 | A Claude window is exhausted mid-study in a child CLIO chose | The child's agent receives `model_exhausted` with the reset time and moves its work to another model |
| M9 | The same exhaustion on a person-chosen root model | A Question offers the fleet's alternatives; nothing switches until answered |
| M10 | A usage poll fails with the network down | The last good snapshot stays, dimmed with its age; the failure is recorded |
| M11 | RLM leaves with `model=` set to a cheaper model | Leaves run on that model and count in the spend rollup under the `rlm` node |
| M12 | A council run with members on two models | The railroad's summary names both models with their token shares and the account window; each member box's hover names its model and source |

**Done** when every phase is implemented, the Codex text match and the hard-coded model lists are gone, M1 to M12 pass against a freshly restarted stack with evidence in the ledger, and the endpoint checks from Phase 0 are recorded with their dates.
