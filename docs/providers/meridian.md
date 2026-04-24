# Running CLIO on your Claude Max subscription via Meridian

CLIO's DSPy runtime speaks the OpenAI chat-completions protocol. [Meridian](https://github.com/rynfar/meridian)
bridges that protocol to Anthropic's SDK using your Claude Max / Pro
OAuth session — so you can point CLIO at Meridian and pay per-seat
instead of per-token.

This recipe targets the `tui-integration` branch of iowarp/clio-agent
and the GACT v0.2 server (`clio-agent-gact`). Nothing here depends on
gact-tui specifically; any OpenAI-compatible consumer works the same
way.

## One-time setup

### 1. Install Meridian

```sh
npm install -g @rynfar/meridian
```

The binary lands on your `$PATH` as `meridian`.

### 2. Authenticate against Claude

If you already run Claude Code locally, Meridian can reuse its OAuth
credentials — point it at the same config directory:

```sh
CLAUDE_CONFIG_DIR="$HOME/.claude" meridian
```

Otherwise register a profile and walk through the browser OAuth flow
once:

```sh
meridian profile add personal
```

Per the upstream docs, each profile lives in an isolated
`~/.config/meridian/profiles/<name>/` and Meridian's token refresh is
handled transparently after that.

### 3. Point CLIO at Meridian

Meridian listens on `127.0.0.1:3456` by default and exposes both the
Anthropic-native and OpenAI-compatible endpoints. CLIO's DSPy path
wants the OpenAI one:

```sh
export CLIO_LM_PROVIDER=openai
export CLIO_LM_API_BASE=http://127.0.0.1:3456/v1
export CLIO_LM_MODEL=claude-haiku-4-5-20251001
export CLIO_LM_API_KEY=x   # any non-empty string; Meridian ignores it
```

The model id comes from Meridian's `/v1/models` list:

```sh
curl -s http://127.0.0.1:3456/v1/models | jq '.data[].id'
# "claude-sonnet-4-6"
# "claude-opus-4-6"
# "claude-haiku-4-5-20251001"
```

**Use Haiku for everything except real production work.** It's
drastically cheaper against your Max quota, and the v0.2 surface
(routing, tool calls, streaming, permissions) all look identical
from the TUI — the model only matters for answer quality. Sonnet and
Opus are available when you need them.

### 4. Smoke test the round trip

```sh
clio-agent --query "hello, name yourself"
```

If that prints a greeting and an expert label, CLIO's DSPy is driving
Meridian and Meridian is driving Claude Max. You're set.

## Running the GACT surface against Meridian

Same env, different entry point:

```sh
clio-agent-gact --port 8100
# Meridian still running on :3456
```

`clio-agent-gact` notices `CLIO_LM_PROVIDER` is set and boots the
real `ClioAgent`; `/v1/health.integrations[]` will report
`agent: ready` instead of `agent: unavailable`.

From gact-tui:

```sh
gact agent deploy clio my-clio
gact connect my-clio
```

`gact agent deploy clio` already inherits your env, so Meridian's
URL + the Haiku pin propagate into the child.

## Troubleshooting

- **`agent: unavailable` in /doctor** — `CLIO_LM_PROVIDER` wasn't set
  in the environment that started `clio-agent-gact`. `gact agent
  deploy` inherits the parent shell's env; double-check with
  `env | grep CLIO_LM_` before deploying.

- **403 from Meridian** — OAuth token expired; run `meridian
  refresh-token` or restart Meridian and let it refresh itself.

- **Meridian prints "Could not verify Claude auth status"** — cosmetic
  if your creds are fine; check with `curl /v1/chat/completions`.
  Refresh via `meridian refresh-token` if real calls 401.

- **Latency spikes** — Meridian serialises concurrent requests behind
  the SDK. For a single interactive TUI that's fine; for concurrent
  load tests flip `MERIDIAN_PASSTHROUGH=1` (passthrough mode) or
  plan on multiple Meridian processes.

## Cost discipline

Every turn through `clio-agent-gact` stamps tokens + cost onto
`Message.cost_usd`, cumulates into the session rollup, and rolls up
again into `/v1/metrics`. Read them back:

```sh
curl -s http://127.0.0.1:8100/v1/metrics | jq '.tokens, .cost'
```

In the TUI, open `/metrics` (palette) for the live modal. The footer
chip shows the per-session cost as turns settle.

Meridian itself passes tokens through from Anthropic's response, so
the numbers are the same ones that would apply to your Max quota.
