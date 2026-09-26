"""OpenAI-chat-message <-> Codex Responses-API conversion (A.5).

Converts DSPy/LiteLLM's OpenAI-shape ``messages``/``tools`` into the Responses
API's ``instructions`` + ``input`` items + function tools, and builds the
per-turn request body the SSE and WebSocket transports both send. Also owns
reasoning-item continuity: with ``store: false`` there is no server-side
state, so the encrypted reasoning items a turn returns
(``include: ["reasoning.encrypted_content"]``) must be threaded back into the
NEXT turn's ``input`` unchanged for the model to keep its chain of thought.

This module is pure data transformation -- no I/O, no session state. Session-
scoped state (which reasoning items are pending, the WebSocket delta-
continuation snapshot) lives in the transport modules, keyed by the stable
CLIO session id from :func:`clio_agent.gact.context.active_session_id`.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "build_request_body",
    "chat_messages_to_responses_input",
    "chat_tools_to_responses_tools",
    "extract_reasoning_items",
    "with_reasoning_items",
]


def _flatten_text(content: Any) -> str:
    """Collapse OpenAI message content (str or a list of parts) into plain text."""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    return str(content)


def _content_to_input_parts(content: Any) -> list[dict[str, Any]]:
    """Convert one user message's content into Responses ``input_text``/``input_image`` parts."""

    if content is None:
        return [{"type": "input_text", "text": ""}]
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    parts: list[dict[str, Any]] = []
    for part in content if isinstance(content, list) else [content]:
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "").strip().lower()
        if part_type == "text" or (not part_type and isinstance(part.get("text"), str)):
            parts.append({"type": "input_text", "text": str(part.get("text") or "")})
            continue
        if part_type in {"image_url", "input_image", "image"}:
            url = part.get("image_url") or part.get("url")
            if isinstance(url, dict):
                url = url.get("url")
            if isinstance(url, str) and url:
                parts.append({"type": "input_image", "image_url": url})
    return parts or [{"type": "input_text", "text": ""}]


def chat_messages_to_responses_input(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Split OpenAI-shape ``messages`` into Responses ``(instructions, input_items)``.

    - ``system``/``developer`` messages join into ``instructions`` (A.5: the
      system prompt belongs there, never in ``input``).
    - ``tool`` messages become ``function_call_output`` items keyed by
      ``call_id``.
    - ``assistant`` messages with ``tool_calls`` become one ``function_call``
      item per call, plus a ``message`` item for any text content.
    - Everything else (``user``, and any unrecognized role) becomes a
      ``message`` item with ``input_text``/``input_image`` parts.
    """

    instructions_parts: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user").strip().lower()
        content = message.get("content")
        if role in {"system", "developer"}:
            text = _flatten_text(content)
            if text:
                instructions_parts.append(text)
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or message.get("call_id") or "")
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _flatten_text(content),
                }
            )
            continue
        if role == "assistant":
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                for call in tool_calls:
                    if not isinstance(call, dict):
                        continue
                    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": str(call.get("id") or ""),
                            "name": str((fn or {}).get("name") or ""),
                            "arguments": str((fn or {}).get("arguments") or "{}"),
                        }
                    )
            text = _flatten_text(content)
            if text:
                input_items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    }
                )
            continue
        input_items.append(
            {"type": "message", "role": "user", "content": _content_to_input_parts(content)}
        )
    return "\n\n".join(instructions_parts), input_items


def chat_tools_to_responses_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Convert OpenAI chat ``tools`` (nested ``function``) into flat Responses function tools."""

    if not tools:
        return []
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        fn = tool.get("function") if isinstance(tool.get("function"), dict) else {}
        name = str((fn or {}).get("name") or "")
        if not name:
            continue
        converted.append(
            {
                "type": "function",
                "name": name,
                "description": str((fn or {}).get("description") or ""),
                "parameters": (fn or {}).get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


def extract_reasoning_items(output_items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return the ``reasoning`` items from a completed response's ``output`` (A.5).

    These are sent back UNCHANGED in the next turn's ``input`` -- CLIO never
    inspects or mutates their ``encrypted_content``.
    """

    if not output_items:
        return []
    return [
        item for item in output_items if isinstance(item, dict) and item.get("type") == "reasoning"
    ]


def _last_assistant_run_start(input_items: list[dict[str, Any]]) -> int | None:
    """Index where the LAST contiguous run of assistant/function-call items begins.

    Reasoning items belong immediately before the assistant turn they informed;
    this locates that turn's start so :func:`with_reasoning_items` can splice
    them back into the right place instead of just prepending them at index 0.
    """

    def _is_assistant_item(item: dict[str, Any]) -> bool:
        return item.get("type") == "function_call" or (
            item.get("type") == "message" and item.get("role") == "assistant"
        )

    last_start: int | None = None
    i, n = 0, len(input_items)
    while i < n:
        if _is_assistant_item(input_items[i]):
            start = i
            while i < n and _is_assistant_item(input_items[i]):
                i += 1
            last_start = start
        else:
            i += 1
    return last_start


def with_reasoning_items(
    input_items: list[dict[str, Any]], reasoning_items: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Splice pending reasoning items back into ``input_items`` (A.5 continuity).

    Inserted immediately before the last assistant/function-call run (the turn
    they were produced for); prepended at the front when no prior assistant
    turn is present yet (the very first continuation).
    """

    if not reasoning_items:
        return input_items
    idx = _last_assistant_run_start(input_items)
    if idx is None:
        return [*reasoning_items, *input_items]
    return [*input_items[:idx], *reasoning_items, *input_items[idx:]]


def build_request_body(
    *,
    model: str,
    input_items: list[dict[str, Any]],
    instructions: str,
    tools: list[dict[str, Any]] | None,
    tool_choice: Any,
    session_id: str,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    """Build one turn's Codex Responses request body (A.5)."""

    body: dict[str, Any] = {
        "model": model,
        "store": False,
        "stream": True,
        "instructions": instructions,
        "input": input_items,
        "tool_choice": tool_choice or "auto",
        "parallel_tool_calls": True,
        "include": ["reasoning.encrypted_content"],
        "prompt_cache_key": session_id,
        "text": {"verbosity": "low"},
    }
    if tools:
        body["tools"] = tools
    if reasoning_effort:
        body["reasoning"] = {"effort": reasoning_effort, "summary": "auto"}
    return body
