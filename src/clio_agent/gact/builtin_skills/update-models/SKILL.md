---
name: update-models
description: Refresh the LM provider model catalogs (codex, claude_code, and every configured HTTP backend) and report what changed per provider.
---

# Update Models

Invoking this skill refreshes clio's per-provider model catalogs against the REAL
current state of each account/backend, and reports exactly what changed. Follow
the two steps below in order.

## Step 1 — Call the refresh tool

Call the `refresh_provider_models` tool. It takes no arguments and probes every
CONFIGURED provider: codex through its app-server's live `model/list`,
claude_code by validating each documented CLI model alias with a trivial turn,
and every HTTP-backed provider (OpenAI, Anthropic, OpenRouter, ALCF/Argonne,
LM Studio, Ollama, local vLLM) with usable credentials through its existing
live models endpoint. It returns JSON shaped like:

```json
{
  "results": [
    {
      "provider": "codex",
      "discovered": [{"id": "...", "name": "...", "description": "..."}],
      "source": "codex_app_server",
      "default_model": "...",
      "added": ["..."],
      "removed": ["..."],
      "unchanged": ["..."],
      "failed_reason": null,
      "rejected": [{"id": "...", "reason": "..."}]
    }
  ]
}
```

A provider whose probe failed reports a non-null `failed_reason` — its
`discovered` list is the PREVIOUS successful discovery (never silently
cleared), and its `added`/`removed` will both be empty since nothing changed.
`rejected` (only present when non-empty) lists candidates the account
DEFINITIVELY does not serve (e.g. a claude_code alias that 404s) even though
the provider's refresh otherwise succeeded — distinct from `failed_reason`,
which means the WHOLE provider's refresh could not complete.

## Step 2 — Report the delta, verbatim

Report back to the user ONE line per provider row in the response, using ONLY the
fields already in the JSON — do not compute, guess, or invent a model id that
isn't present in the response:

- If `failed_reason` is set: say the provider's catalog could not be refreshed
  and why (`failed_reason`), and that its previous model list was kept.
- Otherwise: report `added`, `removed`, and `unchanged` for that provider (as
  their literal id lists — an empty `added` and `removed` means nothing changed
  since the last refresh). If `default_model` is set, mention it as the
  provider's current live default. If `rejected` is present and non-empty,
  mention which candidates were rejected and why.

Keep the report short and scannable (one line or a small table per provider) —
this is a status readback, not an essay.
