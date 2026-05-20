# Adding a new LM provider

clio-agent's provider catalog lives in one place:
[`src/clio_agent/providers/registry.py`][registry]. Adding a new
provider is usually one new `Provider(...)` entry. If the wire isn't
OpenAI / Anthropic-compatible (LiteLLM doesn't speak it natively),
you also write a `CustomLLM`.

This guide walks through both shapes using **Codex** as the worked
example.

## The data model

```python
@dataclass(frozen=True)
class Provider:
    # Catalog identity (what gact's modal shows)
    id: str                  # "openai", "argonne_sophia", "codex"
    label: str               # "OpenAI / ChatGPT"
    description: str         # one paragraph for the modal hint

    # Wire kind — drives LMProviderConfig.provider
    provider_kind: ProviderKind  # "openai" | "anthropic" | "codex" | ...

    # Wire defaults — filled by LMProviderConfig.__post_init__
    api_base: str            # "https://api.openai.com/v1"
    suggested_model: str     # "gpt-4o-mini"
    api_key_default: str = ""

    # Auth
    requires_api_key: bool = True
    auth_method: AuthMethod = "api_key"   # "none" | "api_key" | "oauth"
    api_key_env: str | None = None        # "OPENAI_API_KEY"

    # Capability flags (rare quirks)
    max_tokens_default: int = 32000
    strip_openai_prefix: bool = True

    # Bookkeeping
    is_kind_default: bool = False    # exactly one per kind
    model_catalog: tuple[ModelEntry, ...] = ()
```

## Case 1 — wire is OpenAI- or Anthropic-compatible

Most providers fit here: the upstream speaks the OpenAI chat-
completions protocol (LM Studio, Ollama, OpenRouter, ALCF vLLM, …).
LiteLLM handles the wire. **You write one registry entry, nothing
else.**

Example: imagine adding **`groq`** for Groq Cloud.

1. Open `src/clio_agent/providers/registry.py`. Add an entry to
   `PROVIDERS`:

   ```python
   Provider(
       id="groq",
       label="Groq Cloud",
       description=(
           "Groq's OpenAI-compatible inference cloud. Very fast "
           "tokens-per-second; requires GROQ_API_KEY."
       ),
       provider_kind="openai",
       api_base="https://api.groq.com/openai/v1",
       suggested_model="llama-3.1-70b-versatile",
       api_key_env="GROQ_API_KEY",
       model_catalog=(
           ModelEntry("llama-3.1-70b-versatile", "Llama 3.1 70B", "Default."),
           ModelEntry("llama-3.1-8b-instant", "Llama 3.1 8B", "Faster, smaller."),
       ),
   )
   ```

   Note `provider_kind="openai"` — that's the wire kind. The `id` is
   the catalog handle gact shows; the kind drives LiteLLM model-string
   construction.

2. **Update `PROVIDER_DEFAULTS` test.** If your new wire kind doesn't
   already have an `is_kind_default=True` row (Groq does — `"openai"`
   is already covered), no test change needed. Otherwise add it to
   `test_provider_defaults_keys_match_kinds` in
   `tests/test_core/test_provider_registry.py`.

3. **Done.** `dspy.LM(model="openai/llama-3.1-70b-versatile",
   api_base="https://api.groq.com/openai/v1", api_key=...)` already
   works because Groq speaks openai-compat. gact's modal picks up
   the new preset on the next `GET /v1/providers/lm`.

## Case 2 — wire isn't OpenAI- or Anthropic-compatible

If the upstream speaks JSON-RPC, gRPC, a subprocess, a custom HTTP
shape — anything LiteLLM doesn't natively handle — you write a
`CustomLLM` and register it.

Worked example: **Codex** (`src/clio_agent/providers/codex_litellm.py`).

### 1. Implement the CustomLLM

```python
from litellm import CustomLLM
from litellm.types.utils import Choices, Message, ModelResponse, Usage

class MyProviderLLM(CustomLLM):
    def completion(
        self,
        model: str,
        messages: list,
        api_base: str,
        # ...lots of LiteLLM-side params, see Codex for the full list...
        optional_params: dict,
        **kwargs,
    ) -> ModelResponse:
        clean_model = model.removeprefix("myprovider/")
        # ... do whatever the upstream needs ...
        text = call_my_provider(messages, model=clean_model)
        return ModelResponse(
            id=f"myprov-{uuid.uuid4().hex}",
            choices=[
                Choices(
                    index=0,
                    message=Message(role="assistant", content=text),
                    finish_reason="stop",
                )
            ],
            created=int(time.time()),
            model=f"myprovider/{clean_model}",
            object="chat.completion",
            usage=Usage(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        )

    async def acompletion(self, *args, **kwargs) -> ModelResponse:
        # Easiest: dispatch to the sync path. Add real async if it's
        # cheap (e.g. you already have an async client).
        return self.completion(*args, **kwargs)
```

### 2. Register lazily

```python
_registered: bool = False
_handler: MyProviderLLM | None = None

def ensure_registered() -> None:
    global _registered, _handler
    if _registered:
        return
    import litellm
    _handler = MyProviderLLM()
    litellm.custom_provider_map.append(
        {"provider": "myprovider", "custom_handler": _handler}
    )
    _registered = True
```

### 3. Wire it into `config.py`

Two touchpoints:

**(a) `_ensure_provider_registered()`** — call your `ensure_registered()`
when the user selects this provider:

```python
def _ensure_provider_registered(config: LMProviderConfig) -> None:
    if config.provider == "codex":
        from clio_agent.providers.codex_litellm import ensure_registered
        ensure_registered()
    if config.provider == "myprovider":
        from clio_agent.providers.myprovider_litellm import ensure_registered
        ensure_registered()
```

**(b) `_resolve_model_name()`** — return the `myprovider/<model>` string
so LiteLLM dispatches to your handler:

```python
if config.provider == "myprovider":
    return f"myprovider/{config.model.removeprefix('myprovider/')}"
```

### 4. Add the registry entry

```python
Provider(
    id="myprovider",
    label="My Provider",
    description="...",
    provider_kind="myprovider",       # new wire kind
    api_base="myprovider://local",    # marker; not used by HTTP
    suggested_model="default-model",
    requires_api_key=False,           # or True with api_key_env=...
    auth_method="none",               # or "api_key" / "oauth"
    is_kind_default=True,
    model_catalog=(...),
)
```

### 5. Widen the `ProviderKind` literal

In `registry.py`:

```python
ProviderKind = Literal[
    "lm_studio",
    "ollama",
    "openai",
    "anthropic",
    "argonne",
    "codex",
    "myprovider",      # new
]
```

### 6. Tests

Add `tests/test_core/test_myprovider_provider.py`. At minimum:

- Idempotent registration (`ensure_registered` called twice doesn't
  duplicate the entry in `litellm.custom_provider_map`).
- Mock the upstream and assert `CustomLLM.completion` returns a
  well-formed `ModelResponse`.
- If you have an optional extra, test the "extra not installed" path
  raises an actionable error.

Update `tests/test_core/test_provider_registry.py`'s
`test_provider_defaults_keys_match_kinds` to include the new wire
kind.

### 7. (Optional) Document it

Drop a `docs/providers/myprovider.md` similar to
[`codex.md`](codex.md) — install / auth / troubleshooting. Link it
from the root [`README.md`](../../README.md).

## Design rules

- **All LM calls go through DSPy → LiteLLM.** No raw `requests.post`
  side channels. If LiteLLM can't reach your upstream natively, write
  a `CustomLLM` — don't bypass the abstraction.
- **One source of truth.** The registry is it. `PROVIDER_DEFAULTS`,
  `_LM_PRESETS`, and `_PROVIDER_MODELS` are derived views — never edit
  them directly.
- **Lazy imports.** A `CustomLLM` module is imported only when the
  user selects that provider. Don't import-time-pull heavy
  dependencies from `registry.py` itself.
- **Idempotent registration.** Hot-swapping providers via
  `PUT /v1/providers/lm` should not grow `litellm.custom_provider_map`
  without bound.

[registry]: ../../src/clio_agent/providers/registry.py
