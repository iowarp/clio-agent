# Provider runtime routing and output-cap validation (#1322 / #1323)

Secondary summarization inference resolves from the LM that owns the call. An
empty summarizer identity reuses that LM and adapter exactly. A configured
model-only override retains the caller's provider, endpoint, credential,
transport, and sampling settings. A provider override retains only
provider-independent sampling settings; endpoint, credential reference,
transport, and provider options are resolved for the newly selected provider.
Credentials are materialized at the invocation boundary, and configuration is
read once per invocation.

The configurable summarizer fields are `model`, `provider`, `api_base`,
`credential_ref`, `transport`, and optional `max_tokens` under `summarizer.*`.
Their environment aliases are listed in `docs/ENVIRONMENT.md`. Empty or YAML
null values inherit. Zero omits the client output cap, a positive value is sent
exactly, and a negative value is invalid.

This routing does not classify, repair, retry, replace, or synthesize agent
responses. Model output and tool calls remain governed by the direct ReAct
contract.

## Call-site audit

| Site | Kind | Default route | Explicit route | Regression evidence |
| --- | --- | --- | --- | --- |
| `dspy.BestOfN` / `dspy.Refine` inner attempts | inference | effective expert context | existing model hook override | module-variant tests |
| `module_variants.compile_reward_fn` | inference | effective expert context | existing model hook override | module-variant tests; truncation is re-raised |
| `agents.runtime._summarize_segments_llm` in-turn | inference | effective expert/main context | `summarizer.*` | summarizer identity tests |
| `POST /context/compact` summary | inference | accepted app main LM passed explicitly | `summarizer.*` | manual-compaction routing test |
| `goal.run_llm_judge` | inference | bound caller; accepted app main LM off-context | existing `goal.judge_model` on caller endpoint | goal tests |
| `runtime.ai_review` | inference | bound caller; accepted app main LM off-context | existing `permissions.ai_review_model` on caller endpoint | AI-review tests |
| usage/history/context token readers | bookkeeping | inspect effective bound LM/history; no model call | none | existing usage/context tests |
| provider handshake/model discovery | bookkeeping/network metadata | selected provider identity | provider selection | resolver/handshake tests |

`finish_reason=length` is raised as `LMOutputTruncatedError` with
`details.reason=output_truncated` only after DSPy records response history and
usage. It is a provider failure and is re-raised by the variant reward wrapper
instead of becoming an ordinary score of zero.

## Focused verification

The provider-routing gate covers summarizer inheritance and overrides, concurrent
session isolation, goal and AI-review routing, output-limit propagation,
configuration generation, and truncation handling. Broad matrix validation
remains CI-owned.
