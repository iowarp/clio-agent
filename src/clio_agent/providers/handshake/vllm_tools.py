"""Verify tool calling on a live vLLM server (a deployment fact, not a model one).

vLLM serves tool calls only when it was started with
``--enable-auto-tool-choice`` and a ``--tool-call-parser``; neither is visible
on ``/v1/models`` or ``/version``. A model whose chat template supports tools
(Qwen2.5) therefore looked tool-capable while the server refused every tool
call (seen live on ares: HTTP 400). The handshake runs the tools probe
(:func:`clio_agent.providers.capabilities.dialects.probes.probe_tools`) and
records the result as the deployment's ``tools_enabled``.

The probe is one small generation, so it runs once per server process: the
result is remembered against vLLM's ``process_start_time_seconds`` (from
``/metrics``), and a restarted server -- possibly with different flags -- is
probed again. A server whose start time cannot be read is probed every time
(correct, just not cached). A probe that cannot decide leaves the fact
unknown, logged with its reason.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from clio_agent.providers.api_base import native_root
from clio_agent.providers.capabilities.dialects.probes import ProbeResponse, probe_tools
from clio_agent.providers.capabilities.records import Fact

logger = logging.getLogger(__name__)

_START_TIME = re.compile(r"^process_start_time_seconds\s+([0-9.e+]+)", re.MULTILINE)

#: (api_base, model_id, process start time) -> the probe's verdict.
_verdicts: dict[tuple[str, str, str], Fact[bool]] = {}


async def _process_start(client: Any, root: str) -> str:
    try:
        response = await client.get(f"{root}/metrics")
    except Exception as exc:  # noqa: BLE001 - the cache key is optional; the probe still runs
        logger.info("vllm tools probe: reason=start_time_unreadable root=%s: %s", root, exc)
        return ""
    match = _START_TIME.search(response.text if response.status_code == 200 else "")
    return match.group(1) if match else ""


async def vllm_tools_fact(
    client: Any, api_base: str, model_id: str, api_key: str = ""
) -> Fact[bool]:
    """Whether the vLLM server at ``api_base`` serves tool calls for ``model_id``."""

    root = native_root(api_base).rstrip("/")
    started = await _process_start(client, root)
    key = (root, model_id, started)
    if started and key in _verdicts:
        return _verdicts[key]
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def send(body: dict[str, Any]) -> ProbeResponse:
        response = await client.post(f"{root}/v1/chat/completions", json=body, headers=headers)
        try:
            parsed: Any = response.json()
        except ValueError:
            parsed = None
        return ProbeResponse(response.status_code, parsed)

    fact = await probe_tools(send, model_id=model_id, max_tokens=32)
    if not fact.known:
        logger.warning(
            "vllm tools probe: reason=tools_unverified root=%s model=%s: %s",
            root,
            model_id,
            fact.detail,
        )
    elif started:
        _verdicts[key] = fact
    return fact


__all__ = ["vllm_tools_fact"]
