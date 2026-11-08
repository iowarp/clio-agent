"""Live probe for Inception Mercury diffusion streaming (design: "Companion provider: Mercury 3").

Settles the open questions the diffusion stream handling rests on, against the real
APIs, and writes a JSON summary. Keys come from an env file OUTSIDE any repository
and are never printed, logged or written: every recorded error body is redacted.

    uv run --frozen --no-sync python scripts/probe_inception_diffusion.py \
        --env-file D:/secrets/clio-probe.env [--model mercury-3] [--out .tmp/probe.json]

The env file holds ``INCEPTION_API_KEY=...`` and/or ``OPENROUTER_API_KEY=...``; a
section whose key is missing is reported as skipped.

Questions answered (``summary.json`` -> ``answers``):

* does the last chunk carry ``finish_reason``, or only ``[DONE]`` mark the end;
* do ``tool_calls`` diffuse as snapshots or arrive once at the end;
* what a not-yet-denoised position looks like (first-step character histogram);
* does reasoning text diffuse, and does usage still arrive with ``include_usage``;
* does DSPy's stream assembly concatenate the snapshots into the ``Response`` text,
  and does CLIO's snapshot owner (``call_lm``) settle on the last snapshot;
* does OpenRouter forward ``diffusing`` for ``inception/mercury-3`` and accept
  ``reasoning.effort`` ``instant`` (and/or ``minimal``).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import httpx

INCEPTION_BASE = "https://api.inceptionlabs.ai/v1"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_TIMEOUT = httpx.Timeout(120.0, connect=20.0)
_PROSE_PROMPT = (
    "In about 120 words, explain why diffusion language models can generate text in "
    "parallel. Plain prose, no lists."
)
_TOOL_PROMPT = "What is the weather in Chicago right now? Use the tool."
_WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


def load_env_file(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines (``export`` and quotes tolerated); values are never echoed."""
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


class Redactor:
    """Replace every known secret in recorded text."""

    def __init__(self, secrets: list[str]) -> None:
        self._secrets = [s for s in secrets if s]

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


def _step_is_snapshot(previous: str, current: str) -> bool | None:
    from clio_agent.gact.agents.clio_react_canvas import step_is_snapshot  # noqa: PLC0415

    return step_is_snapshot(previous, current)


async def sse_case(
    client: httpx.AsyncClient,
    redact: Redactor,
    *,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> dict[str, Any]:
    """Stream one chat completion and record every chunk's shape (never the key)."""
    started = time.monotonic()
    record: dict[str, Any] = {"request": {k: v for k, v in body.items() if k != "messages"}}
    steps: list[dict[str, Any]] = []
    contents: list[str] = []
    reasoning: list[str] = []
    tool_call_steps: list[Any] = []
    usage: Any = None
    done_seen = False
    try:
        async with client.stream("POST", url, headers=headers, json=body) as response:
            record["status"] = response.status_code
            if response.status_code != 200:
                record["error"] = redact((await response.aread()).decode("utf-8", "replace"))[:800]
                return record
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done_seen = True
                    continue
                chunk = json.loads(data)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                delta = (choices[0].get("delta") if choices else None) or {}
                finish = choices[0].get("finish_reason") if choices else None
                content = delta.get("content") or ""
                extra = sorted(k for k in delta if k not in {"role", "content", "tool_calls"})
                for key in extra:
                    value = delta.get(key)
                    if isinstance(value, str) and value:
                        reasoning.append(value)
                if delta.get("tool_calls"):
                    tool_call_steps.append(delta["tool_calls"])
                if content:
                    contents.append(content)
                steps.append(
                    {
                        "t_ms": int((time.monotonic() - started) * 1000),
                        "content_len": len(content),
                        "content_head": content[:80],
                        "finish_reason": finish,
                        "tool_calls": bool(delta.get("tool_calls")),
                        "extra_delta_keys": extra,
                        "choices": len(choices),
                        "usage": bool(chunk.get("usage")),
                    }
                )
    except httpx.HTTPError as exc:
        record["error"] = redact(f"{type(exc).__name__}: {exc}")
        return record
    record["steps"] = steps
    record["analysis"] = _analyse(contents, reasoning, tool_call_steps, steps, usage, done_seen)
    return record


def _analyse(
    contents: list[str],
    reasoning: list[str],
    tool_call_steps: list[Any],
    steps: list[dict[str, Any]],
    usage: Any,
    done_seen: bool,
) -> dict[str, Any]:
    verdicts = [_step_is_snapshot(a, b) for a, b in zip(contents, contents[1:], strict=False)]
    judged = [v for v in verdicts if v is not None]
    first = contents[0] if contents else ""
    histogram = collections.Counter(first).most_common(6)
    content_steps = [s for s in steps if s["content_len"]]
    last_content_finish = content_steps[-1]["finish_reason"] if content_steps else None
    return {
        "content_steps": len(contents),
        "judged_steps": len(judged),
        "snapshot_like_steps": sum(judged),
        "content_lengths": [len(c) for c in contents][:64],
        "last_content": contents[-1][:2000] if contents else "",
        "concatenated_len": sum(len(c) for c in contents),
        "first_step_char_histogram": histogram,
        "first_step_head": first[:200],
        "finish_reason_on_last_content_chunk": last_content_finish,
        "finish_reasons": sorted({str(s["finish_reason"]) for s in steps if s["finish_reason"]}),
        "done_marker": done_seen,
        "usage": usage,
        "reasoning_chunks": len(reasoning),
        "reasoning_snapshot_like": [
            _step_is_snapshot(a, b) for a, b in zip(reasoning, reasoning[1:], strict=False)
        ][:32],
        "tool_call_chunks": len(tool_call_steps),
        "tool_call_deltas": tool_call_steps[:8],
    }


def _messages(prompt: str) -> list[dict[str, str]]:
    return [{"role": "user", "content": prompt}]


async def probe_inception(key: str, model: str, redact: Redactor) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = f"{INCEPTION_BASE}/chat/completions"
    out: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        models = await client.get(f"{INCEPTION_BASE}/models", headers=headers)
        out["models"] = {
            "status": models.status_code,
            "body": json.loads(models.text) if models.status_code == 200 else redact(models.text),
        }
        base = {"model": model, "stream": True, "stream_options": {"include_usage": True}}
        cases = {
            "plain_stream": {**base, "messages": _messages(_PROSE_PROMPT)},
            "diffusing_instant": {
                **base,
                "messages": _messages(_PROSE_PROMPT),
                "diffusing": True,
                "reasoning_effort": "instant",
            },
            "diffusing_high": {
                **base,
                "messages": _messages(_PROSE_PROMPT),
                "diffusing": True,
                "reasoning_effort": "high",
            },
            "diffusing_tool_call": {
                **base,
                "messages": _messages(_TOOL_PROMPT),
                "diffusing": True,
                "tools": [_WEATHER_TOOL],
                "tool_choice": "auto",
            },
            "effort_minimal_accepted": {
                **base,
                "messages": _messages("Say hi."),
                "reasoning_effort": "minimal",
            },
        }
        for name, body in cases.items():
            out[name] = await sse_case(client, redact, url=url, headers=headers, body=body)
    return out


async def probe_openrouter(key: str, model: str, redact: Redactor) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    url = f"{OPENROUTER_BASE}/chat/completions"
    slug = f"inception/{model}"
    out: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        listing = await client.get(f"{OPENROUTER_BASE}/models")
        row = next(
            (m for m in listing.json().get("data", []) if m.get("id") == slug),
            None,
        )
        out["model_row"] = (
            {
                "context_length": row.get("context_length"),
                "top_provider": row.get("top_provider"),
                "supported_parameters": row.get("supported_parameters"),
            }
            if row
            else None
        )
        base = {
            "model": slug,
            "stream": True,
            "stream_options": {"include_usage": True},
            "messages": _messages(_PROSE_PROMPT),
        }
        cases = {
            "plain_stream": base,
            "diffusing_top_level": {**base, "diffusing": True},
            "effort_instant": {**base, "diffusing": True, "reasoning": {"effort": "instant"}},
            "effort_minimal": {**base, "diffusing": True, "reasoning": {"effort": "minimal"}},
        }
        for name, body in cases.items():
            out[name] = await sse_case(client, redact, url=url, headers=headers, body=body)
    return out


def probe_dspy_path(key: str, model: str, redact: Redactor) -> dict[str, Any]:
    """DSPy's stream assembly and CLIO's snapshot owner on the native ``inception/`` route."""
    import anyio  # noqa: PLC0415
    import dspy  # noqa: PLC0415
    from dspy.lm15 import Message, Request, TextPart  # noqa: PLC0415

    from clio_agent.gact.agents.clio_react_stream_call import call_lm  # noqa: PLC0415
    from clio_agent.gact.reasoning_extract import chunk_delta, field_text  # noqa: PLC0415
    from clio_agent.lm.engines.lm_loop import run_on_lm_loop  # noqa: PLC0415
    from clio_agent.lm.request_builder import sendable_lm_kwargs  # noqa: PLC0415
    from clio_agent.lm.request_config import config_from_lm_kwargs  # noqa: PLC0415

    lm = dspy.LM(
        f"inception/{model}",
        api_key=key,
        cache=False,
        extra_body={"diffusing": True, "reasoning_effort": "instant"},
    )
    request = Request(
        model=lm.model,
        messages=(Message.user(_PROSE_PROMPT),),
        config=config_from_lm_kwargs(sendable_lm_kwargs(lm)),
    )

    def text_of(response: Any) -> str:
        return "".join(p.text for p in response.message.parts if isinstance(p, TextPart))

    async def raw() -> tuple[list[str], str]:
        send, receive = anyio.create_memory_object_stream(math.inf)
        texts: list[str] = []

        async def consume() -> None:
            async with receive:
                async for chunk in receive:
                    delta = chunk_delta(chunk)
                    if delta is not None and (text := field_text(delta)):
                        texts.append(text)

        async with anyio.create_task_group() as group:
            group.start_soon(consume)
            try:
                with dspy.context(send_stream=send):
                    response = await lm.acall(request)
            finally:
                await send.aclose()
        return texts, text_of(response)

    out: dict[str, Any] = {}
    try:
        texts, response_text = run_on_lm_loop(raw)
        out["dspy_stream"] = {
            "chunks_with_text": len(texts),
            "response_equals_last_chunk": bool(texts) and response_text == texts[-1],
            "response_equals_concatenation": response_text == "".join(texts),
            "response_len": len(response_text),
            "last_chunk_len": len(texts[-1]) if texts else 0,
        }
    except Exception as exc:  # noqa: BLE001 - the probe records every failure
        out["dspy_stream"] = {"error": redact(f"{type(exc).__name__}: {exc}")[:800]}
    try:
        final = text_of(call_lm(lm, request))
        out["clio_call_lm"] = {"final_len": len(final), "final_head": final[:400]}
    except Exception as exc:  # noqa: BLE001 - the probe records every failure
        out["clio_call_lm"] = {"error": redact(f"{type(exc).__name__}: {exc}")[:800]}
    return out


def answers(summary: dict[str, Any]) -> dict[str, Any]:
    """The design's open questions, read off the recorded cases."""
    inc = summary.get("inception") or {}
    diff = (inc.get("diffusing_instant") or {}).get("analysis") or {}
    high = (inc.get("diffusing_high") or {}).get("analysis") or {}
    tool = (inc.get("diffusing_tool_call") or {}).get("analysis") or {}
    dspy_path = (summary.get("dspy") or {}).get("dspy_stream") or {}
    orr = summary.get("openrouter") or {}
    return {
        "snapshot_shape_confirmed": (
            diff.get("judged_steps", 0) > 0
            and diff.get("snapshot_like_steps") == diff.get("judged_steps")
        ),
        "finish_reason_on_last_content_chunk": diff.get("finish_reason_on_last_content_chunk"),
        "done_marker": diff.get("done_marker"),
        "first_step_char_histogram": diff.get("first_step_char_histogram"),
        "usage_with_include_usage": diff.get("usage") is not None,
        "reasoning_chunks_high_effort": high.get("reasoning_chunks"),
        "reasoning_snapshot_like": high.get("reasoning_snapshot_like"),
        "tool_call_chunks": tool.get("tool_call_chunks"),
        "minimal_effort_status": (inc.get("effort_minimal_accepted") or {}).get("status"),
        "dspy_concatenates_snapshots": dspy_path.get("response_equals_concatenation"),
        "openrouter_diffusing_snapshot_like": (
            ((orr.get("diffusing_top_level") or {}).get("analysis") or {}).get(
                "snapshot_like_steps"
            )
        ),
        "openrouter_effort_instant_status": (orr.get("effort_instant") or {}).get("status"),
        "openrouter_effort_minimal_status": (orr.get("effort_minimal") or {}).get("status"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--model", default="mercury-3")
    parser.add_argument("--out", type=Path, default=Path(".tmp/probe-inception/summary.json"))
    parser.add_argument("--skip-dspy", action="store_true")
    args = parser.parse_args(argv)

    env = load_env_file(args.env_file)
    inception_key = env.get("INCEPTION_API_KEY", "")
    openrouter_key = env.get("OPENROUTER_API_KEY", "")
    redact = Redactor([inception_key, openrouter_key])
    summary: dict[str, Any] = {"model": args.model}
    if inception_key:
        summary["inception"] = asyncio.run(probe_inception(inception_key, args.model, redact))
        if not args.skip_dspy:
            summary["dspy"] = probe_dspy_path(inception_key, args.model, redact)
    else:
        summary["inception"] = {"skipped": "INCEPTION_API_KEY not in the env file"}
    if openrouter_key:
        summary["openrouter"] = asyncio.run(probe_openrouter(openrouter_key, args.model, redact))
    else:
        summary["openrouter"] = {"skipped": "OPENROUTER_API_KEY not in the env file"}
    summary["answers"] = answers(summary)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(redact(json.dumps(summary, indent=2, default=str)), encoding="utf-8")
    print(json.dumps(summary["answers"], indent=2, default=str))
    print(f"summary written to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
