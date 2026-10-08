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

Only the tools probe exists: it is the one fact a server's self-report can
contradict for a model whose template supports tools (vLLM refuses tool calls
unless it was started with a tool-call parser). It runs from the vLLM
handshake (:mod:`clio_agent.providers.handshake.vllm_tools`). The vision,
reasoning and parameter probes were never called and were removed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.records import Fact, unknown

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


#: The tools probe's retry budget when its first reply is truncated before any call.
TOOLS_PROBE_RETRY_MAX_TOKENS = 512


def _truncated_without_calls(response: ProbeResponse) -> bool:
    """Whether a 2xx reply stopped at ``finish_reason="length"`` with no tool call yet."""
    if response.status_code >= 400 or not isinstance(response.body, dict):
        return False
    choices = response.body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    message = _message(response) or {}
    return choices[0].get("finish_reason") == "length" and not message.get("tool_calls")


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


async def probe_tools(send: Any, *, model_id: str, max_tokens: int = 64) -> Fact[bool]:
    """Tools probe (brief 5.3): one ``record_sum(a, b)`` tool call.

    ``send`` is an async ``Callable[[dict], Awaitable[ProbeResponse]]`` the
    caller builds (already carrying the endpoint URL, auth header and dialect
    spelling of ``tools=[...]``) -- this function only decides the request
    BODY and how to read the result, so it works unchanged against every
    OpenAI-shaped dialect.
    """
    for budget in (max_tokens, max(max_tokens, TOOLS_PROBE_RETRY_MAX_TOKENS)):
        body = {
            "model": model_id,
            "messages": [{"role": "user", "content": "What is 21 + 21? Use the record_sum tool."}],
            "tools": [RECORD_SUM_TOOL],
            "tool_choice": "auto",
            "max_tokens": budget,
            "temperature": 0,
        }
        try:
            response = await send(body)
        except Exception:  # noqa: BLE001 - a failed probe is unknown, never False
            return unknown("probe: tools request failed")
        # A reply cut off by the budget before any call says nothing about tool
        # support: a reasoning model spends a small budget thinking first (Qwen3
        # on vLLM, F016). Retry once with room to finish, else stay unknown.
        if not _truncated_without_calls(response):
            break
    else:
        return unknown(
            f"probe: response truncated (finish_reason=length) before any tool call "
            f"at max_tokens={budget}"
        )
    if response.status_code >= 400:
        blamed = _error_field_name(response)
        if response.status_code == 400 and blamed and "tool" in blamed.casefold():
            # The server refused the tools field itself (vLLM started without
            # --enable-auto-tool-choice: '"auto" tool choice requires
            # --enable-auto-tool-choice and --tool-call-parser to be set') --
            # the same rule as a parameter probe whose 400 names the field.
            return Fact(False, "probe", _now_iso(), f"probe: server refused tools: {blamed}")
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
            arguments = (
                json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
            )
        except ValueError:
            continue
        if isinstance(arguments, dict) and "a" in arguments and "b" in arguments:
            return Fact(
                True, "probe", _now_iso(), "probe: record_sum tool call with valid arguments"
            )
    return Fact(
        False,
        "probe",
        _now_iso(),
        "probe: tool_calls present but arguments did not match the schema",
    )
