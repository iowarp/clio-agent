# Provider runtime routing and output-cap validation (#1322 / #1323)

Secondary inference resolves from the LM that owns the call. Empty repairer and
summarizer identities reuse that LM and adapter exactly when sampling is unchanged.
A repair retry with a temperature change rebuilds a hooked LM from the caller's
identity and uses its matching adapter. Configuration is read once per invocation.
A model-only override retains the caller's provider, endpoint,
credential, transport and sampling settings. A provider override retains only
provider-independent sampling settings; endpoint, credential reference, transport
and provider options are resolved for the newly selected provider. Credentials are
materialized at the invocation boundary.

Typed-output repair captures the rejected attempt's effective routed LM on the
specific hooked wrapper that made the call. The capture is cleared before every
invocation, is set only when a real target is dispatched, and is isolated between
concurrent wrappers. Synthetic responses and hook failures therefore cannot reuse
an older call's identity. Default repair retries preserve the wrapper and adapter,
run model hooks again, and apply the next retry temperature to the captured caller;
an explicit `repairer.*` identity still overrides that caller-relative route.

The configurable role fields are `model`, `provider`, `api_base`, `credential_ref`,
`transport`, and optional `max_tokens`, under `repairer.*` and `summarizer.*`.
Their environment aliases are listed in `docs/ENVIRONMENT.md`. Empty or YAML null
values inherit. Zero omits the client output cap, a positive value is sent exactly,
and a negative value is invalid.

## Call-site audit

| Site | Kind | Default route | Explicit route | Regression evidence |
| --- | --- | --- | --- | --- |
| `gact.agents.builders` typed-output retry | inference | rejected attempt LM, next retry temperature, hooks, and matching adapter | `repairer.*` | production malformed HTTP, hook isolation, and secondary identity tests |
| `reactv2.reforce_submit_over_retained_history` | inference | rejected extract LM; retained History remains input | `repairer.*` | retained-history repair test |
| `dspy.BestOfN` / `dspy.Refine` inner attempts | inference | effective expert context | existing model hook override | module-variant tests |
| `module_variants.compile_reward_fn` | inference | effective expert context | existing model hook override | module-variant tests; truncation is re-raised |
| `agents.runtime._summarize_segments_llm` in-turn | inference | effective expert/main context | `summarizer.*` | summarizer identity tests |
| `POST /context/compact` summary | inference | accepted app main LM passed explicitly (sessions currently share this default) | `summarizer.*` | manual-compaction routing test |
| `goal.run_llm_judge` | inference | bound caller; accepted app main LM off-context | existing `goal.judge_model` on caller endpoint | goal tests |
| `runtime.ai_review` | inference | bound caller; accepted app main LM off-context | existing `permissions.ai_review_model` on caller endpoint | AI-review tests |
| usage/history/context token readers | bookkeeping | inspect effective bound LM/history; no model call | none | existing usage/context tests |
| provider handshake/model discovery | bookkeeping/network metadata | selected provider identity | provider selection | resolver/handshake tests |

`finish_reason=length` is raised as `LMOutputTruncatedError` with
`details.reason=output_truncated` only after DSPy records response history and usage.
It is a provider failure, is not eligible for schema repair, and is re-raised by the
variant reward wrapper instead of becoming an ordinary score of zero.

## Focused verification

- Repair, hook, and secondary-routing focused gate, including production blueprint
  malformed HTTP and routed/synthetic/hook-failed identity isolation: 30 passed.
- Related routing, repair, output-limit, configuration, AI-review, and generated
  documentation gate: 150 passed.
- Secondary routing and generated-document drift follow-up: 33 passed.
- Focused mypy: no issues in `hooked_lm.py`, `secondary.py`, and `builders.py`.
- Factory-failure preservation plus production repair and effective-target isolation:
  4 passed; the original exception object escapes when LM creation fails.
