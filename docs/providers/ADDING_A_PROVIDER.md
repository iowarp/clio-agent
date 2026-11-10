# Adding a new LM provider

clio-agent's provider catalog lives in one place:
[`src/clio_agent/providers/catalog.py`][catalog]. Adding a new
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
LiteLLM handles the wire. **You write one catalog entry, nothing
else.**

Example: imagine adding **`groq`** for Groq Cloud.

1. Open `src/clio_agent/providers/catalog.py`. Add an entry to
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
   `tests/test_core/test_provider_catalog.py`.

3. **Done.** `dspy.LM(model="openai/llama-3.1-70b-versatile",
   api_base="https://api.groq.com/openai/v1", api_key=...)` already
   works because Groq speaks openai-compat. gact's modal picks up
   the new preset on the next `GET /v1/providers/lm`.

## Case 2 — wire isn't OpenAI- or Anthropic-compatible

If the upstream speaks JSON-RPC, gRPC, a subprocess, a custom HTTP
shape — anything LiteLLM doesn't natively handle — you write a
`CustomLLM` and register it.

> **Subscription SDK/CLI transports don't use this shape any more.**
> Codex, Claude Code and GitHub Copilot are DSPy 3.4 engine pairs that
> `create_lm()` builds directly, with no LiteLLM in the call path. A new
> provider that drives a vendor SDK or CLI with the user's own plan
> follows [Case 3](#case-3--a-subscription-sdk-or-cli-transport-engine-pair).
> The `CustomLLM` walkthrough below is kept for plain non-HTTP wires.

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

### 4. Add the catalog entry

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

In `catalog.py`:

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

Update `tests/test_core/test_provider_catalog.py`'s
`test_provider_defaults_keys_match_kinds` to include the new wire
kind.

### 7. (Optional) Document it

Drop a `docs/providers/myprovider.md` similar to
[`codex.md`](codex.md) — install / auth / troubleshooting. Link it
from the root [`README.md`](../../README.md).

## Case 3 — a subscription SDK or CLI transport (engine pair)

Use this when the provider runs the vendor's own SDK or CLI with the
user's plan (Claude Code, Codex, GitHub Copilot). The vendor runtime is
a **model transport only**: CLIO's agent loop and MCP/tool gateway stay
the only tool path. Worked example: **GitHub Copilot**
(`src/clio_agent/providers/copilot/`, [copilot.md](copilot.md)).

### 1. Catalog and dependency

- Add the kind to `ProviderKind` in `providers/catalog_types.py` and a
  `Provider(...)` row in `catalog.py` with `auth_method="subscription"`,
  `requires_api_key=False`, `inner_loop_owner="clio"` and
  `suggested_model=""` when the list is discovered live.
- Add the kind to `CLIO_CUSTOM_LITELLM_PROVIDERS` in
  `providers/custom_transports.py`, so capability lookups never ask
  LiteLLM about it.
- Put the SDK in an optional extra in `pyproject.toml` (refresh
  `uv.lock`) and register it in `providers/dependencies.py`
  (`PROVIDER_SUPPORT` and an `ensure_<kind>_support` that stages any
  runtime at install time; a turn never downloads).
- If the SDK must move forward between CLIO releases, add it to
  `providers/components/registry.py` **and**
  `USER_UPDATABLE_COMPONENTS` in `scripts/check_bundle_matches_lock.py`
  (a test fails when they drift), then `components/client_binary.py`,
  `components/verify.py` (`_VERIFIERS`) and `support_restore.py`.

### 2. Isolation options

Build the SDK client and session kwargs in ONE module
(`providers/copilot/options.py`) and switch off, explicitly, every
vendor feature that can act on the machine or read ambient state:
built-in/custom/MCP tools, skills, memory, hooks, instruction files,
config discovery, git, session stores, autopilot and compaction. Never
let the SDK read the machine's own vendor login. Pin every flag in a
test (`tests/test_providers/test_copilot_options.py`).

### 3. Engine pair and sessions

- `providers/<kind>/engine.py`: an `Async<Kind>Engine` / `<Kind>Engine`
  pair implementing `complete`/`stream` over `dspy.lm15.Request` and
  returning a typed `Response`. Render tools and messages with
  `lm/engines/text_tools.py`, and stream and assemble the reply with
  the Claude Code engine's turn helpers (`_Turn`, `_assemble` in
  `providers/claude_code_engine.py`), which split the text from the
  fenced `tool_calls` block.
- `providers/<kind>/sessions.py`: one pooled runtime per GACT session
  with a reaper, release on session terminal status
  (`providers/session_lifecycle.py`) and `on_client_dropped` signalling,
  and one vendor session per conversation through
  `providers/claude_code_conversation.py` (append-only deltas, typed
  reset reasons on `provider.stateful`).
- Errors: transport faults and stalls raise `dspy.lm15` types (DSPy
  retries them); a refused sign-in, missing seat, disabled model or
  exhausted plan raise typed CLIO errors with a stable `reason`
  (`providers/copilot/errors.py`), never retried.

### 4. Factory and effort

- `lm/factory.py`: a `_<kind>_lm()` that builds
  `dspy.LM(f"<kind>/{model}", engine=..., async_engine=...)`, and one
  dispatch line in `create_lm()`.
- Effort: a dialect in `providers/capabilities/dialects/<kind>.py`
  built only from what the vendor reports, plus its entries in
  `providers/capabilities/endpoint.py` and `lm/dialect_wire.py`.

### 5. Sign-in, readiness and the bind gate

- A sign-in adapter `gact/routes/provider_auth_<kind>.py` with
  `start`/`status`/`complete`, registered in the kind maps of
  `gact/routes/provider_auth.py`. The generic gact-tui sign-in panel
  renders whatever `start` returns (`device` and/or `browser`), so no
  new UI is needed. Copilot reuses CLIO's GitHub account: the adapter
  hands sign-in to the storage GitHub device flow and registers a token
  source at startup.
- A readiness function in the `readiness_by_kind` map of
  `gact/routes/providers.py`, and the subscription gate in
  `gact/providers/bind_configuration.py` (`_apply_subscription_gate`)
  so `PUT /v1/providers/lm` refuses a signed-out or unready provider
  with a typed code.
- The handshake (`providers/handshake/__init__.py`, `cli_catalog.py`),
  `_CLI_CATALOG_KINDS` in `gact/provider_catalog.py`, the doctor probe
  (`runtime/lm_provider_probe.py`) and the stream failure messages
  (`gact/stream_failures.py`, `providers/terminal_signal_catalog.py`).

### 6. Live model discovery

`providers/model_discovery/<kind>.py` lists the account's models and
returns a `ProviderDiscoveryResult` under its own source in
`model_discovery/overlay.py`; wire it into `model_discovery/refresh.py`.
Rows the account cannot use are `rejected` with a typed code, never
silently dropped. A malformed overlay is reported, never treated as
empty.

### 7. Usage and quota

Plug into `providers/usage/` (account id in `capture.py`, a reader in
`readers.py`, a poller module) and add the plan-limit reason to
`gact/fleet/exhaustion.py`. A missing or renamed quota field is an
unreadable reading, never zero.

### 8. gact-tui

In `web/src/lib/provider-presentation.ts`, add the provider's label,
summary, logo id (artwork rules in `web/public/provider-logos/README.md`)
and, for a service-managed client, its vendor in
`providerManagedClientVendor`. That enables the client-update panel and
the update-before-refresh path. Bump `external/gact-tui`.

### 9. Tests and docs

- A fake SDK (`tests/_fake_copilot_sdk.py`) installed in `sys.modules`,
  so no test needs the vendor package, a network or a sign-in.
- Options, engine/session, auth/discovery, route/readiness and usage
  tests (`tests/test_providers/test_copilot_*.py`,
  `tests/test_gact/test_copilot_provider_routes.py`).
- `docs/providers/<kind>.md` (setup, prerequisites outside CLIO,
  troubleshooting) and a row in [README.md](README.md).

## Design rules

- **All LM calls go through DSPy.** No raw `requests.post` side
  channels. HTTP providers go through LiteLLM (natively or via a
  `CustomLLM`); subscription SDK/CLI transports are DSPy engine pairs
  (Case 3). Don't bypass either abstraction.
- **One source of truth.** The catalog is it. `PROVIDER_DEFAULTS`,
  `_LM_PRESETS`, and `_PROVIDER_MODELS` are derived views — never edit
  them directly.
- **Lazy imports.** A `CustomLLM` module is imported only when the
  user selects that provider. Don't import-time-pull heavy
  dependencies from `catalog.py` itself.
- **Idempotent registration.** Hot-swapping providers via
  `PUT /v1/providers/lm` should not grow `litellm.custom_provider_map`
  without bound.

[catalog]: ../../src/clio_agent/providers/catalog.py
