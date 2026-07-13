"""LiteLLM response construction for the Claude Code provider.

Split out of :mod:`clio_agent.providers.claude_code_litellm` (#891) so the
provider module stays under its file-size ratchet while the #891 stream-audit
instrumentation lands. This holds the pure translation from a Claude Code
result (text + raw SDK usage dict) into a LiteLLM ``ModelResponse`` — no I/O,
no SDK calls.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from litellm.types.utils import Choices, Message, ModelResponse, Usage


def build_model_response(
    *,
    text: str,
    model: str,
    usage_payload: dict[str, Any] | None = None,
    request_id: str | None = None,
) -> ModelResponse:
    """Wrap a Claude Code result in a LiteLLM ``ModelResponse``.

    The three input-side token counts the SDK reports (fresh input, cache
    creation, cache read) are summed into LiteLLM's single ``prompt_tokens`` —
    the cache breakdown is preserved only in the ``provider.call_usage`` audit
    row, not here.

    Args:
        text: The assistant response text.
        model: The clean model name (rendered back as ``claude_code/<model>``).
        usage_payload: The raw SDK usage dict, or ``None`` when unavailable.
        request_id: Optional response id; a random one is minted when absent.

    Returns:
        A populated LiteLLM ``ModelResponse`` with a single assistant choice.
    """
    usage_payload = usage_payload or {}
    prompt_tokens = int(usage_payload.get("input_tokens", 0) or 0)
    prompt_tokens += int(usage_payload.get("cache_creation_input_tokens", 0) or 0)
    prompt_tokens += int(usage_payload.get("cache_read_input_tokens", 0) or 0)
    completion_tokens = int(usage_payload.get("output_tokens", 0) or 0)
    return ModelResponse(
        id=request_id or f"claude-code-{uuid.uuid4().hex}",
        choices=[
            Choices(
                index=0,
                message=Message(role="assistant", content=text),
                finish_reason="stop",
            )
        ],
        created=int(time.time()),
        model=f"claude_code/{model}",
        object="chat.completion",
        usage=Usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


__all__ = ["build_model_response"]
