# LM providers

clio-agent supports several LM provider backends. The catalog lives in
[`src/clio_agent/providers/registry.py`][registry] — a single source of
truth shared by `LMProviderConfig.__post_init__` (wire defaults) and
the `GET /v1/providers/lm` endpoint (gact's modal). Adding a new
provider is one new `Provider(...)` entry; see
[ADDING_A_PROVIDER.md](ADDING_A_PROVIDER.md) for the walkthrough.

Every provider routes through **DSPy → LiteLLM**. There are no raw
HTTP side channels; if a backend doesn't work through LiteLLM
directly, it goes through a LiteLLM `CustomLLM` (see [codex](codex.md)
and [claude_code](claude_code.md) for CLI-backed examples).

## Supported providers

| id | wire kind | auth | When to use it |
|---|---|---|---|
| `lm_studio` | `lm_studio` | none | Local models via LM Studio; model auto-discovered if blank |
| `ollama` | `ollama` | none | Local models via Ollama |
| `openai` | `openai` | `OPENAI_API_KEY` | Direct OpenAI API (per-token billing) |
| `anthropic` | `anthropic` | `ANTHROPIC_API_KEY` | Direct Anthropic API (per-token billing) |
| `openrouter` | `openai` | api_key | OpenAI-compat gateway to many providers; free tier available |
| [`codex`](codex.md) | `codex` | `codex login` | Your ChatGPT / Codex subscription, no per-token cost |
| [`claude_code`](claude_code.md) | `claude_code` | `claude login` | Your Claude Code subscription, no Anthropic API key |
| `argonne_sophia` | `argonne` | Globus OAuth | ALCF Sophia inference gateway (vLLM) |
| `argonne_metis` | `argonne` | Globus OAuth | ALCF Metis inference gateway (gpt-oss-120b) |
| `argonne_local_vllm` | `openai` | none | Local OpenAI-compatible vLLM endpoint |

## Switching providers

Three ways, in increasing scope:

1. **Mid-session** (TUI). `Ctrl+S` → Settings → Model →
   `Change provider…`. Modal pops, pick a preset, save. The next turn
   uses the new LM. No restart.
2. **Per server boot** (env vars). `CLIO_LM_PROVIDER`,
   `CLIO_LM_API_BASE`, `CLIO_LM_MODEL`, `CLIO_LM_API_KEY` (and the
   provider-specific knobs documented per-provider).
3. **Programmatic** (Python). Construct an `LMProviderConfig` and pass
   it to `create_lm()` / `create_planner_lm()`.

## How a provider call lands

```
user prompt
  ↓
ClioAgent.action_planner          (dspy.Predict with AgentActionSignature)
  ↓
dspy.context(lm=self._planner_lm)  (per-request LM scope)
  ↓
dspy.LM.forward()                 (LiteLLM-compatible model string)
  ↓
litellm.completion()              (matches provider prefix → handler)
  ↓
either:
  - native LiteLLM handler        (openai/, anthropic/, …)
  - CustomLLM in custom_provider_map  (codex/, claude_code/)
```

The `CustomLLM` handlers for codex and Claude Code are registered lazily
at `create_lm()` time, so CLI-specific code never loads for installs
that do not use those providers.

## Provider-specific docs

- [Codex (subscription)](codex.md)
- [Claude Code (subscription)](claude_code.md)

## Local reasoning model profiles

CLIO applies a planner-specific profile for known local reasoning GGUFs
served through LM Studio or Ollama. For Qwopus/Qwen-style model ids
(`qwopus`, `qwen3`, `qwen-3`, `qwen35`, `qwen-3.5`), the planner LM is
made deterministic and gets at least 4096 planner tokens even if the
general answer cap is lower. This keeps hidden reasoning tokens from
starving the planner's required JSON action output.

Validated baseline:

- `qwopus3.5-9b-v3` via LM Studio ROCm
- 32k context loaded
- direct LM Studio smoke: `LMSTUDIO_QWOPUS_OK`
- CLIO smoke: `CLIO_QWOPUS_OK`
- 26,056-token prompt push: `CTX32K_QWOPUS_OK`

## Authoring a new provider

[ADDING_A_PROVIDER.md](ADDING_A_PROVIDER.md) — step-by-step walkthrough
with codex as the worked example.

[registry]: ../../src/clio_agent/providers/registry.py
