"""The managed Ollama context default applied after the model pull."""

from __future__ import annotations

import asyncio
import json

import pytest

from clio_agent.gact.infrastructure.models import CommandResult, CommandSpec
from clio_agent.gact.infrastructure.ollama_context_apply import (
    gpu_available_bytes,
    ollama_context_hook,
)

GIB = 1 << 30
MODEL_INFO = {
    "general.architecture": "qwen3",
    "qwen3.context_length": 262144,
    "qwen3.block_count": 36,
    "qwen3.attention.head_count_kv": 8,
    "qwen3.attention.key_length": 128,
    "qwen3.attention.value_length": 128,
}
LOG = (
    'time=2026-10-08T09:40:01 level=INFO source=types.go:42 msg="inference compute" '
    'id=GPU-1 library=CUDA name="NVIDIA A100-SXM4-40GB" total="39.4 GiB" available="38.6 GiB"\n'
)


def test_gpu_available_is_read_from_the_startup_log() -> None:
    assert gpu_available_bytes(LOG) == int(38.6 * GIB)
    assert gpu_available_bytes("no gpu line") is None


class FakeTarget:
    """Answers the hook's commands like a running Ollama server would."""

    def __init__(self, show: dict, create_ok: bool = True) -> None:
        self.show = show
        self.create_ok = create_ok
        self.created: list[dict] = []

    async def __call__(self, spec: CommandSpec) -> CommandResult:
        url = next((arg for arg in spec.args if arg.startswith("http")), "")
        if url.endswith("/api/show"):
            out = json.dumps(self.show)
        elif url.endswith("/api/tags"):
            out = json.dumps({"models": [{"name": "qwen3:4b", "size": 2_600_000_000}]})
        elif url.endswith("/api/create"):
            self.created.append(json.loads(spec.stdin))
            out = '{"status":"success"}' if self.create_ok else '{"error":"boom"}'
        else:
            out = LOG
        return CommandResult(exit_code=0, stdout=out, stderr="")


def _run(target: FakeTarget) -> dict[str, str]:
    logs = CommandSpec(program="sh", args=["-c", "true"])
    hook = ollama_context_hook(11434, "qwen3:4b", logs, windows=False)
    return asyncio.run(hook(target, lambda _message: None))


def test_hook_recreates_the_model_under_its_name_with_the_capped_context() -> None:
    target = FakeTarget({"model_info": MODEL_INFO})
    chosen = _run(target)
    tokens = int(chosen["effective.context_length"])
    assert 4096 <= tokens < 262144 and tokens % 4096 == 0
    assert "capped" in chosen["effective.context_reason"]
    assert target.created == [
        {"model": "qwen3:4b", "from": "qwen3:4b", "parameters": {"num_ctx": tokens}}
    ]


def test_hook_leaves_ollama_default_when_the_model_reports_no_context() -> None:
    target = FakeTarget({"model_info": {"general.architecture": "x"}})
    assert _run(target) == {}
    assert target.created == []


def test_hook_failure_is_reported_not_swallowed() -> None:
    with pytest.raises(RuntimeError, match="Could not set the context"):
        _run(FakeTarget({"model_info": MODEL_INFO}, create_ok=False))
