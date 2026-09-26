"""Active deployment probes (model-capabilities brief 5.3).

Run only when self-report leaves a fact unknown, and only during discovery or
refresh for local/self-hosted endpoints -- NEVER per turn. Each probe is one
small chat-completion request (``max_tokens`` in the 16-64 range). A probe
measures a MODEL and a SERVER together, so every result here is a
:class:`~clio_agent.providers.capabilities.records.DeploymentCapabilities` fact
(``source="probe"``), never written back into the model record.

A failed probe (transport error, non-2xx with no clear "field rejected"
signal) records :func:`~clio_agent.providers.capabilities.records.unknown`,
never ``False`` -- "the endpoint refused this" and "we couldn't tell" are
different facts, and the brief is explicit that a failure must not collapse
into the negative answer.

Ports clio-coder's ``probe/reasoning.ts`` field-detection contract
(``reasoning_content`` / ``reasoning`` / ``reasoning_text``); the tools,
vision and parameter probes are fresh code against the brief's own spec (no
clio-coder ``probe/tool-call.ts`` exists in this repo's clio-coder checkout to
port).
"""

from __future__ import annotations

import base64
import json
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.records import Fact, unknown

#: A tiny, valid PNG: 16x16, solid red (brief 5.3: "a 16x16 PNG"). Built once at
#: import time rather than shipped as a binary fixture, so the exact pixel
#: color used in the probe prompt and the bytes sent are always in sync.
_PNG_SIZE = 16
_PNG_COLOR = (220, 20, 20)  # a red the model should describe as "red"


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))


def _solid_color_png(size: int, rgb: tuple[int, int, int]) -> bytes:
    """Build a minimal, valid solid-color PNG with no external image library."""
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    row = b"\x00" + bytes(rgb) * size
    raw = row * size
    idat = _png_chunk(b"IDAT", zlib.compress(raw, level=6))
    iend = _png_chunk(b"IEND", b"")
    return header + ihdr + idat + iend


_VISION_PROBE_PNG_B64 = base64.b64encode(_solid_color_png(_PNG_SIZE, _PNG_COLOR)).decode("ascii")

#: Fields a chat-completion response's message may carry non-empty reasoning
#: text under, tried in this order (clio-coder ``probe/reasoning.ts``).
_REASONING_FIELDS: tuple[str, ...] = ("reasoning_content", "reasoning", "reasoning_text")

#: The brief's tool probe: ``record_sum(a, b)``.
RECORD_SUM_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "record_sum",
        "description": "Record the sum of two numbers.",
        "parameters": {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
            },
            "required": ["a", "b"],
        },
    },
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ProbeResponse:
    """The bare shape every probe needs from a chat-completion HTTP response.

    Callers adapt their own HTTP client's response object to this (a thin,
    typed seam) so the probes below never depend on ``httpx`` directly -- the
    same probes run against any OpenAI-shaped endpoint.
    """

    status_code: int
    body: Any  # parsed JSON on 2xx, or an error-shaped dict on non-2xx


def _message(response: ProbeResponse) -> dict[str, Any] | None:
    if not isinstance(response.body, dict):
        return None
    choices = response.body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    return message if isinstance(message, dict) else None


def _error_field_name(response: ProbeResponse) -> str | None:
    """The field name a 400 response blames, when it names one (brief 5.3 parameters probe)."""
    if not isinstance(response.body, dict):
        return None
    error = response.body.get("error")
    text = ""
    if isinstance(error, dict):
        text = str(error.get("message") or error.get("param") or "")
    elif isinstance(error, str):
        text = error
    return text or None


async def probe_tools(
    send: Any, *, model_id: str, max_tokens: int = 64
) -> Fact[bool]:
    """Tools probe (brief 5.3): one ``record_sum(a, b)`` tool call.

    ``send`` is an async ``Callable[[dict], Awaitable[ProbeResponse]]`` the
    caller builds (already carrying the endpoint URL, auth header and dialect
    spelling of ``tools=[...]``) -- this function only decides the request
    BODY and how to read the result, so it works unchanged against every
    OpenAI-shaped dialect.
    """
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": "What is 21 + 21? Use the record_sum tool."}],
        "tools": [RECORD_SUM_TOOL],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    try:
        response = await send(body)
    except Exception:  # noqa: BLE001 - a failed probe is unknown, never False
        return unknown("probe: tools request failed")
    if response.status_code >= 400:
        return unknown(f"probe: tools request rejected (HTTP {response.status_code})")
    message = _message(response)
    if message is None:
        return unknown("probe: tools response had no message")
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return Fact(False, "probe", _now_iso(), "probe: no tool_calls in response")
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        arguments_raw = function.get("arguments") if isinstance(function, dict) else None
        try:
            arguments = json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
        except ValueError:
            continue
        if isinstance(arguments, dict) and "a" in arguments and "b" in arguments:
            return Fact(True, "probe", _now_iso(), "probe: record_sum tool call with valid arguments")
    return Fact(False, "probe", _now_iso(), "probe: tool_calls present but arguments did not match the schema")


async def probe_vision(send: Any, *, model_id: str, max_tokens: int = 64) -> Fact[bool]:
    """Vision probe (brief 5.3): a 16x16 solid-color PNG, "what color is this square?"."""
    body = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What color is this square? Answer with one word."},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{_VISION_PROBE_PNG_B64}"},
                    },
                ],
            }
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    try:
        response = await send(body)
    except Exception:  # noqa: BLE001 - a failed probe is unknown, never False
        return unknown("probe: vision request failed")
    if response.status_code >= 400:
        # A 400 here is ambiguous (malformed request vs. "this model can't see
        # images") -- unlike the parameter probe, there is no reliable way to
        # confirm the rejection was IMAGE-specific, so this stays unknown
        # rather than guessing a negative (brief: "a failed probe records
        # unknown, never False").
        return unknown(f"probe: vision request rejected (HTTP {response.status_code})")
    message = _message(response)
    content = message.get("content") if message else None
    if isinstance(content, str) and content.strip():
        return Fact(True, "probe", _now_iso(), "probe: non-error response named a color")
    return unknown("probe: vision response had no content")


async def probe_reasoning(
    send: Any, *, model_id: str, control: str, control_value: Any, max_tokens: int = 64
) -> Fact[bool]:
    """Reasoning probe (brief 5.3): request thinking through ``control`` and check for reasoning text.

    ``control``/``control_value`` are the wire field + value for ONE control
    the endpoint record offers (brief 5.5's :class:`ThinkingDecision.control`) --
    the caller runs this once per available control when more than one exists.
    """
    body: dict[str, Any] = {
        "model": model_id,
        "messages": [{"role": "user", "content": "What is 2+2? Think briefly, then answer."}],
        "max_tokens": max_tokens,
        "temperature": 0,
        control: control_value,
    }
    try:
        response = await send(body)
    except Exception:  # noqa: BLE001 - a failed probe is unknown, never False
        return unknown(f"probe: reasoning request via {control!r} failed")
    if response.status_code >= 400:
        return unknown(f"probe: reasoning request via {control!r} rejected (HTTP {response.status_code})")
    message = _message(response)
    if message is None:
        return unknown("probe: reasoning response had no message")
    for field_name in _REASONING_FIELDS:
        value = message.get(field_name)
        if isinstance(value, str) and value.strip():
            return Fact(True, "probe", _now_iso(), f"probe: non-empty {field_name!r} via {control!r}")
    return Fact(False, "probe", _now_iso(), f"probe: no reasoning field populated via {control!r}")


async def probe_parameter(
    send: Any, *, model_id: str, param_name: str, param_value: Any, max_tokens: int = 32
) -> Fact[bool]:
    """Parameter probe (brief 5.3): send one optional parameter in its own request.

    A 400 that names ``param_name`` means rejected (``False``). Any other
    non-error outcome counts as accepted, but "accepted silently" (no field
    named in an error, or no error at all) is recorded with a distinct detail
    so a caller can tell confirmed support from a server that just didn't
    validate: ``detail="probe: accepted silently"``.
    """
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": "Say OK."}],
        "max_tokens": max_tokens,
        param_name: param_value,
    }
    try:
        response = await send(body)
    except Exception:  # noqa: BLE001 - a failed probe is unknown, never False
        return unknown(f"probe: parameter {param_name!r} request failed")
    if response.status_code >= 400:
        blamed = _error_field_name(response)
        if blamed and param_name in blamed:
            return Fact(False, "probe", _now_iso(), f"probe: {param_name!r} rejected: {blamed}")
        # A 400 for an unrelated reason (bad model id, auth) is inconclusive.
        return unknown(f"probe: parameter {param_name!r} request failed for an unrelated reason (HTTP 400)")
    return Fact(True, "probe", _now_iso(), "probe: accepted silently")


__all__ = [
    "RECORD_SUM_TOOL",
    "ProbeResponse",
    "probe_parameter",
    "probe_reasoning",
    "probe_tools",
    "probe_vision",
]
