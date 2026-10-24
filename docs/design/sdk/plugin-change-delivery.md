# How a plugin change reaches the model

Adding or removing a plugin mid-conversation changes the agent's system prompt and
tool list. Those sit at the front of every request, and so of the provider's prompt
cache. Every provider hashes the cache in order (tools, then system, then messages),
so rewriting the front throws away the cache for the whole conversation behind it,
and the next call is billed and timed as a fresh prompt.

So every add or remove in the + menu opens one warning before anything changes:

> **Warning.** Changing plugins changes the system prompt, which invalidates the
> prompt cache, so the next model call may cost more. Do you want to proceed?
>
> **Yes** · **Yes, after compaction** · **Yes, as an injection** · **No**

The answer is the `apply` field of `PATCH /v1/sessions/{sid}/agent`. The UI has
no default: the person picks each time, for the + menu and for switching agents.
API callers that omit `apply` get `now` (a script pinning an agent on a new
session, where nothing is cached yet). The owner module is
`src/clio_agent/gact/agents/prompt_basis.py`.

| Button | `apply` | What happens |
| --- | --- | --- |
| Yes | `now` | The next request carries the new prompt and tools. The cache is rebuilt, so that call costs more. |
| Yes, after compaction | `compact` | The session is compacted now and the change goes in with it. Compaction replaces most of the history with a summary, so the rebuilt prefix is short. With nothing to compact, the change applies at once. |
| Yes, as an injection | `note` | The prompt keeps the resolution the model already has (the *prompt basis*, session key `agent_prompt_basis`) until the next compaction. Each turn carries a `plugin_change` injection describing the difference, shown in the transcript with its details on hover. |
| No | (no request) | Nothing changes. |

Under `note`:

* **Removed plugins** stay in the prompt and their tools stay callable. The note
  asks the model not to use them. Removing does not shrink the context until the
  compaction, and compliance depends on the model.
* **Added plugins**: their prompt fragments travel in the note, and their skills
  load now through `load_skill`. Their tools are built and run if called. The
  request's native tool list keeps only what the cached prompt had, and the note
  introduces each new tool by name, arguments and description. Use is less
  reliable than a listed tool, and the compaction makes it a listed tool.
* **Hooks** follow the pinned resolution at once.
* **MCP servers** are the union of the basis's and the pinned resolution's, so
  anything the model can still see or was told about can run.
* **Any compaction** (manual, automatic, or `apply: compact`) settles the basis on
  the pinned resolution, and the note stops.

## Rendering the note

The default, used for every provider, folds the note into the conversation as a user
message headed `[clio: plugin_change]`. Every chat template accepts it.

A native operator message is not a safe default:

* Strict open-model templates refuse a system message anywhere but first. Qwen3's
  template raises, and vLLM and llama.cpp return HTTP 400.
* lm15's chat-completions profiles map a `developer` message to `system` for most
  OpenAI-compatible endpoints.

On those servers, tool changes have no cache-safe form at all: the template renders
the tool list into the leading system block.

## Native paths (next step)

Detect the capability per model and use it where it exists. Keep the textual note
everywhere else.

* **Anthropic** (Claude Opus 5.5 / 5 / 4.8, Sonnet 5.5, Haiku 5.5, Fable and Mythos
  5.x; not Sonnet 5).
  * The note becomes a mid-conversation `role: "system"` message, which carries
    operator authority and leaves the prefix cached.
  * Tool changes become `tool_addition` / `tool_removal` blocks, behind beta
    headers.
  * Placement rules apply: the message follows a user turn and never sits between
    a `tool_use` and its `tool_result`.
  * The `claude_code` provider is the Anthropic Messages dialect, so it can carry
    this too.
  * Sources: [mid-conversation system messages](https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages),
    [tool search](https://platform.claude.com/docs/agents-and-tools/tool-use/tool-search-tool).
* **OpenAI Responses**.
  * The note becomes a `developer` message.
  * Added tools use an `additional_tools` input item, or tool search with
    `defer_loading`; either appends at the end and keeps the prefix.
  * Removal sets `allowed_tools` per request while the `tools` list stays fixed.
  * Sources: [prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching),
    [tool search](https://developers.openai.com/api/docs/guides/tools-tool-search).
* **Open models** (vLLM, llama.cpp, LM Studio). Keep the textual note and keep the
  tool list fixed per basis.
  * A template verified to render later system messages could opt in. Rendering
    the tokens does not mean the model gives them system priority.
  * Check that the rendered prompt really is append-only (vLLM's
    `/v1/chat/completions/render`), because a template that rewrites earlier turns
    misses the cache whatever we send.
