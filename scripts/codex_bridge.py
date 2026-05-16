"""OpenAI-compatible HTTP shim for the Codex app-server SDK.

CLIO's LM dispatch already speaks the OpenAI chat-completions wire shape
(via Meridian/OpenRouter/OpenAI/LM Studio). This bridge pretends to be
yet another openai-compatible endpoint, so CLIO can drive Codex through
the same provider/model picker without learning a new protocol.

Usage
-----

1. Install the Codex app-server SDK:

       cd <openai/codex>/sdk/python
       uv pip install -e .

   (You also need the `codex` binary on PATH, which the SDK launches as
   a subprocess.)

2. Run the bridge:

       python scripts/codex_bridge.py --port 18900

3. Configure CLIO via the TUI's Settings → Change provider… modal:

       Provider:    openai-compatible
       API base:    http://127.0.0.1:18900/v1
       Model:       gpt-5.4 (or whatever `codex` exposes via /v1/models)
       API key:     (leave blank)

   Or via the wire:

       PUT /v1/providers/lm
       { "provider": "openai-compatible",
         "model":    "gpt-5.4",
         "api_base": "http://127.0.0.1:18900/v1",
         "api_key":  "" }

Caveats
-------

- Codex threads are stateful; OpenAI chat completions are stateless
  multi-message. We run each request as a fresh Codex thread using the
  *last* user message as the input. System prompts are ignored — Codex
  has its own personality + reasoning machinery and tends to override
  injected system text anyway.
- Streaming SSE is not implemented (Codex SDK has its own event stream
  that doesn't map 1:1 to OpenAI deltas). CLIO falls back to non-stream
  for any provider that doesn't surface deltas, so this still works.
- Token usage in the response is best-effort: Codex reports it via a
  separate notification; we expose what's available, zero out the rest.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import uuid
from typing import Any

try:
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "codex_bridge.py needs fastapi + uvicorn. Install via:\n"
        "  uv pip install 'fastapi>=0.104' 'uvicorn>=0.24'"
    ) from exc


def _import_codex_sdk():
    """Import codex_app_server, with an actionable error if missing."""
    try:
        from codex_app_server import (
            AppServerConfig,
            Codex,
        )
        return AppServerConfig, Codex
    except ImportError as exc:
        raise SystemExit(
            "codex_app_server not importable. Clone openai/codex and run:\n"
            "  cd <codex>/sdk/python && uv pip install -e .\n"
            f"(original error: {exc})"
        ) from exc


def build_app(*, model_default: str = "gpt-5.4") -> FastAPI:
    """FastAPI app implementing the OpenAI subset CLIO uses."""
    AppServerConfig, Codex = _import_codex_sdk()

    app = FastAPI(title="codex-openai-bridge", version="0.1.0")
    app.state.model_default = model_default
    app.state.codex_config = AppServerConfig()
    log = logging.getLogger("codex_bridge")

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        """Enumerate Codex models. Falls back to a single-row stub if the
        Codex binary is unreachable so /v1/models still works for
        diagnostic purposes."""
        try:
            with Codex(config=app.state.codex_config) as codex:
                listing = codex.models()
            return {
                "object": "list",
                "data": [
                    {
                        "id": m.id,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "openai",
                    }
                    for m in listing.data
                ],
            }
        except Exception as exc:  # noqa: BLE001
            log.warning("Codex models() failed, returning stub: %s", exc)
            return {
                "object": "list",
                "data": [
                    {
                        "id": app.state.model_default,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "openai",
                    }
                ],
            }

    @app.post("/v1/chat/completions")
    async def chat_completions(payload: dict[str, Any]) -> JSONResponse:
        """OpenAI chat-completions endpoint backed by a one-shot Codex
        thread. We grab the most recent user message, run it on a fresh
        thread, and return the final assistant text."""
        messages = payload.get("messages") or []
        if not messages:
            raise HTTPException(
                status_code=422, detail="messages must be a non-empty list"
            )

        # Last user message wins. System content is concatenated upfront
        # because some prompt-engineered system instructions are useful
        # context (Codex still has its own bias, but giving it a hint
        # doesn't hurt).
        system_parts: list[str] = []
        last_user = ""
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            if isinstance(content, list):  # multi-part
                content = "".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and isinstance(p.get("text"), str)
                )
            if role == "system":
                system_parts.append(content)
            elif role == "user":
                last_user = content

        prompt = last_user
        if system_parts:
            prompt = "\n\n".join(system_parts) + "\n\n---\n\n" + prompt
        if not prompt.strip():
            raise HTTPException(
                status_code=422, detail="no user message content found"
            )

        model = payload.get("model") or app.state.model_default

        try:
            with Codex(config=app.state.codex_config) as codex:
                thread = codex.thread_start(model=model)
                result = thread.run(prompt)
        except Exception as exc:  # noqa: BLE001
            log.exception("Codex run failed")
            raise HTTPException(
                status_code=502,
                detail=f"codex run failed: {exc!r}",
            ) from exc

        text = (result.final_response or "").strip()
        return JSONResponse(
            content={
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }
        )

    @app.get("/")
    async def root() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "codex-openai-bridge",
            "endpoints": ["/v1/models", "/v1/chat/completions"],
        }

    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="codex_bridge",
        description="Expose the Codex app-server SDK as an OpenAI-compatible HTTP endpoint.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=18900, type=int)
    parser.add_argument(
        "--model-default",
        default="gpt-5.4",
        help="Model id used when /v1/chat/completions request omits one.",
    )
    parser.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = build_app(model_default=args.model_default)
    print(
        f"codex-openai-bridge listening on http://{args.host}:{args.port}\n"
        f"  POST /v1/chat/completions  ← OpenAI-compatible\n"
        f"  GET  /v1/models             ← Codex models\n",
        file=sys.stderr,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
