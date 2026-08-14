---
name: update-models
description: Refresh the LM provider model catalogs (codex, claude_code, and every configured HTTP backend) and report what changed per provider.
---

# Update Models

Invoking this skill refreshes clio's per-provider model catalogs against the REAL
current state of each account/backend, and reports exactly what changed. Follow
the two steps below in order.

## Step 1 — Call the refresh action

Call `POST /v1/providers/models/refresh` on this GACT server's own base URL (the
same server you are running inside of — its loopback address, typically
`http://127.0.0.1:8100` unless the deployment configured a different port; use
whatever base URL you already know this server answers on if that default
doesn't work). Use whichever tool you have that can make this call (a shell
`curl`/`Invoke-WebRequest`, or an equivalent HTTP-capable tool) — an empty POST
body is correct, no arguments are needed.

This one call probes every configured provider: codex through its app-server's
live `model/list`, claude_code by validating each documented CLI model alias with
a trivial turn, and every HTTP-backed provider (OpenAI, Anthropic, OpenRouter,
ALCF/Argonne, LM Studio, Ollama, local vLLM) through its existing live models
endpoint. It returns JSON shaped like:

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
      "failed_reason": null
    }
  ]
}
```

A provider whose probe failed reports a non-null `failed_reason` — its
`discovered` list is the PREVIOUS successful discovery (never silently
cleared), and its `added`/`removed` will both be empty since nothing changed.

## Step 2 — Report the delta, verbatim

Report back to the user ONE line per provider row in the response, using ONLY the
fields already in the JSON — do not compute, guess, or invent a model id that
isn't present in the response:

- If `failed_reason` is set: say the provider's catalog could not be refreshed
  and why (`failed_reason`), and that its previous model list was kept.
- Otherwise: report `added`, `removed`, and `unchanged` for that provider (as
  their literal id lists — an empty `added` and `removed` means nothing changed
  since the last refresh). If `default_model` is set, mention it as the
  provider's current live default.

Keep the report short and scannable (one line or a small table per provider) —
this is a status readback, not an essay.
