# Lab user notes — v0.3.1 rehearsal

Walked through the lab-user setup from scratch as a sanity check
before the v0.3.1 announcement. Documenting timings + rough edges
discovered.

## Clean install (Linux, Python 3.12+, Go 1.26+)

| Step | Time | Notes |
|---|---|---|
| `git clone iowarp/clio-agent` (`tui-integration` branch, --depth 1) | <2s | nothing surprising |
| `git clone iowarp/gact-tui` (`clio` branch, --depth 1) | <2s | |
| `cd clio-agent && uv sync` | ~3s | uv has the wheel cache warm; first-time install on a fresh box may take longer (lots of LM/MCP deps) |
| `cd ../gact-tui/tui && go build -o gact .` | ~3s | |
| `clio-agent-gact --port 17900` (boot) | ~3-5s | clean boot; `/v1/health` returns 28/30 caps before LM is wired |
| `PUT /v1/providers/lm` (configure) | <1s | accepts any openai-compatible preset |
| First chat turn | ~5-10s | depends on the upstream LM (Meridian + Claude haiku ≈ 5s) |

## Rough edges (none release-blocking, all documented)

- **Chat agent fallback was not unconditional** — the original v0.3.1
  RC had `is_local_openai_compatible_backend` gating the
  `_direct_chat_completion` recovery path, which excluded
  `provider="openai-compatible"` (the canonical preset for Meridian +
  OpenRouter). Symptom: `"I encountered an issue with the chat
  expert"` on simple prompts. **Fixed** in commit `9f1c701` — the
  fallback now runs for every provider, since the recovery path is
  generic (requests.post against the configured api_base).

- **`_direct_chat_completion` was sending the doubled provider prefix**
  to Meridian (e.g. `openai/claude-haiku-4-5` instead of
  `claude-haiku-4-5`). Meridian rejected. **Fixed** in commit `15fc2aa`
  — the prefix is stripped before the HTTP call.

- **OpenRouter free-tier rate limits.** The shared key in tests works
  for low-traffic verification, but heavy use (e.g. running the full
  16-test integration suite back-to-back via OpenRouter) hits 429s
  pretty quickly. The suite swaps to OpenRouter only for the
  streaming-temporal-distribution test; other tests use Meridian /
  Claude. If a lab user wants to drive everything through OpenRouter,
  expect occasional rate-limit failures.

- **Meridian streaming is buffered.** `data: [DONE]` only — Meridian
  doesn't actually proxy SSE chunks from upstream. Live per-token
  streaming requires OpenRouter or a direct-API setup.

## Smoke flow that worked end-to-end

```bash
git clone --branch tui-integration --depth 1 git@github.com:iowarp/clio-agent.git
git clone --branch clio --depth 1 git@github.com:iowarp/gact-tui.git
cd clio-agent && uv sync
cd ../gact-tui/tui && go build -o /tmp/gact-smoke .

# Boot the server.
cd ../../clio-agent && nohup .venv/bin/clio-agent-gact --port 17900 > /tmp/clio.log 2>&1 &

# Configure with whatever provider the lab member uses.
curl -X PUT http://127.0.0.1:17900/v1/providers/lm \
  -d '{"provider":"openai","model":"gpt-4o-mini",
       "api_base":"https://api.openai.com/v1","api_key":"$OPENAI_API_KEY",
       "temperature":0.0,"max_tokens":256}'

# Drive a turn.
SID=$(curl -s -X POST http://127.0.0.1:17900/v1/sessions -d '{"title":"smoke"}' \
       | python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])')
curl -X POST "http://127.0.0.1:17900/v1/sessions/$SID/messages" \
  -d '{"parts":[{"type":"text","text":"hello in one sentence"}]}'

# Read it back.
sleep 5
curl -s "http://127.0.0.1:17900/v1/sessions/$SID/messages" | python3 -m json.tool

# Or open the TUI.
GACT_BACKEND=http://127.0.0.1:17900 /tmp/gact-smoke
```

Verified result: `tokens={'input': 3, 'output': 8}, cost=$4.3e-05`,
text starts with "I'm CLIO, specialized in scientific data..."

## Recommendations for the lab announcement

- Mention all three providers (OpenAI / Meridian / OpenRouter) in the
  intro — the chat path now works for all three.
- Point users at `docs/CAPABILITIES_MATRIX.md` so they know what the
  binary can actually do.
- The 28/30 number (only LSP + voice false) is real — every other
  capability flag is verified.
