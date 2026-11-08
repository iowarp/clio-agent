"""Drive one delegation over a Clio Coder surface and fold its stream into a ``DelegationResult``.

``run_headless`` uses ``clio-coder run --json-events terminal``: notices stream, and one terminal
``turn_end`` frame carries the answer, outcome and safety record. ``run_acp`` holds an ACP
session open for a task plus follow-up prompts and streams tool progress as it happens.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clio_agent.runtime.clio_coder.acp import (
    AcpConnection,
    PermissionHandler,
    reject_permissions,
)
from clio_agent.runtime.clio_coder.launch import (
    ClioCoderError,
    ClioCoderInstall,
    InstanceHome,
    SpawnedInstance,
    spawn_instance,
)
from clio_agent.runtime.clio_coder.results import (
    OUTCOME_CANCELED,
    OUTCOME_COST_CAPPED,
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    OUTCOME_TIMED_OUT,
    DelegationResult,
    ToolActivity,
    outcome_for_exit,
    summarize_usage,
)

#: Receives normalized progress events: ``{"kind": "session"|"notice"|"tool"|"text", ...}``.
EventSink = Callable[[dict[str, Any]], None]

#: Extra wall-clock the parent allows past Clio Coder's own ``--timeout`` before it stops the tree.
_WATCHDOG_SLACK_SECONDS = 30.0
_CANCEL_SETTLE_SECONDS = 10.0
_STDERR_TAIL_BYTES = 4000

_STOP_REASON_OUTCOMES = {
    "end_turn": OUTCOME_SUCCEEDED,
    "cancelled": OUTCOME_CANCELED,
    "max_tokens": OUTCOME_FAILED,
    "max_turn_requests": OUTCOME_FAILED,
    "refusal": OUTCOME_FAILED,
}


@dataclass
class RunOptions:
    """Per-delegation knobs that map onto Clio Coder flags and ACP session options."""

    autonomy: str = "default"
    model: str | None = None
    target: str | None = None
    read_only: bool = False
    timeout: float = 600.0
    max_cost_usd: float | None = None
    allow_tools: Sequence[str] | None = None
    extra_args: Sequence[str] = field(default_factory=tuple)


def _emit(sink: EventSink | None, event: dict[str, Any]) -> None:
    if sink is not None:
        sink(event)


async def _drain_stderr(instance: SpawnedInstance, buffer: bytearray) -> None:
    stream = instance.process.stderr
    if stream is None:
        return
    while chunk := await stream.read(65536):
        buffer.extend(chunk)
        del buffer[: max(0, len(buffer) - _STDERR_TAIL_BYTES)]


def headless_args(task: str, cwd: Path, options: RunOptions) -> list[str]:
    args = ["run", "--json-events", "terminal", "--cwd", str(cwd)]
    args += ["--autonomy", options.autonomy, "--timeout", str(max(1, int(options.timeout)))]
    if options.target:
        args += ["--target", options.target]
    if options.model:
        args += ["--model", options.model]
    if options.read_only:
        args.append("--read-only")
    if options.max_cost_usd is not None:
        args += ["--max-cost-usd", str(options.max_cost_usd)]
    if options.allow_tools is not None:
        args += ["--allow-tools", ",".join(options.allow_tools) or "none"]
    return [*args, *options.extra_args, task]


def _fold_headless_frame(
    result: DelegationResult, frame: dict[str, Any], sink: EventSink | None
) -> None:
    kind = frame.get("type")
    if kind == "session":
        result.session_id = frame.get("id")
        result.model = frame.get("model")
        _emit(sink, {"kind": "session", "sessionId": result.session_id, "model": result.model})
    elif kind == "notice":
        result.notices.append(str(frame.get("text", "")))
        _emit(sink, {"kind": "notice", "level": frame.get("level"), "text": frame.get("text")})
    elif kind == "tool_execution_end":
        status = {"error": "failed", "blocked": "blocked"}.get(
            str(frame.get("outcome")), "failed" if frame.get("isError") else "completed"
        )
        result.tools.append(ToolActivity(str(frame.get("toolName")), status))
        _emit(sink, {"kind": "tool", "name": frame.get("toolName"), "status": status})
    elif kind == "agent_end":
        result.usage = summarize_usage(frame.get("usage"))
    elif kind == "turn_end" and "exitCode" in frame:
        result.text = str(frame.get("text") or "")
        result.exit_code = frame.get("exitCode")
        result.outcome = str(frame.get("outcome") or outcome_for_exit(result.exit_code))
        result.outcome_detail = frame.get("outcomeDetail")
        result.safety = frame.get("safety") or {}
        if frame.get("error"):
            result.error = {"message": frame["error"]}


async def run_headless(
    install: ClioCoderInstall,
    home: InstanceHome,
    cwd: Path,
    task: str,
    options: RunOptions,
    sink: EventSink | None = None,
) -> DelegationResult:
    """One headless Clio Coder turn; cancelling the caller stops the owned tree."""
    instance = await spawn_instance(
        install, headless_args(task, cwd, options), cwd=cwd, home=home, stdin_pipe=False
    )
    result = DelegationResult(surface="headless", outcome=OUTCOME_FAILED)
    result.confinement = instance.confinement
    stderr = bytearray()
    stderr_task = asyncio.create_task(_drain_stderr(instance, stderr))

    async def consume() -> None:
        stdout = instance.process.stdout
        while stdout is not None and (line := await stdout.readline()):
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                result.notices.append(line.decode("utf-8", "replace").strip()[:500])
                continue
            _fold_headless_frame(result, frame, sink)

    try:
        await asyncio.wait_for(consume(), options.timeout + _WATCHDOG_SLACK_SECONDS)
        await instance.process.wait()
    except TimeoutError:
        result.outcome, result.outcome_detail = OUTCOME_TIMED_OUT, "parent_watchdog"
    finally:
        exit_code = await instance.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(stderr_task, 5.0)
        result.stderr_tail = stderr.decode("utf-8", "replace")
    if result.exit_code is None:
        result.exit_code = exit_code
        if result.outcome_detail != "parent_watchdog":
            result.outcome = outcome_for_exit(exit_code)
    if result.exit_code == 4:
        result.outcome = OUTCOME_COST_CAPPED
    return result


@dataclass
class _AcpState:
    result: DelegationResult
    sink: EventSink | None
    text: list[str] = field(default_factory=list)
    tools: dict[str, ToolActivity] = field(default_factory=dict)

    def on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method != "session/update":
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text":
                self.text.append(str(content.get("text", "")))
                _emit(self.sink, {"kind": "text", "text": content.get("text", "")})
        elif kind in ("tool_call", "tool_call_update"):
            # ACP chunks carry no message boundary; a tool call between them is one.
            if self.text and not self.text[-1].endswith("\n"):
                self.text.append("\n\n")
            call_id = str(update.get("toolCallId"))
            tool = self.tools.setdefault(
                call_id, ToolActivity(str(update.get("name") or update.get("title")), "pending")
            )
            tool.status = str(update.get("status") or tool.status)
            tool.title = str(update.get("title") or tool.title)
            _emit(self.sink, {"kind": "tool", "name": tool.name, "status": tool.status})
        elif kind == "session_info_update":
            notice = (update.get("_meta") or {}).get("clio-coder/notice")
            if isinstance(notice, dict) and notice.get("text"):
                self.result.notices.append(str(notice["text"]))
                _emit(self.sink, {"kind": "notice", **notice})


def _add_usage(total: dict[str, Any], more: dict[str, Any]) -> dict[str, Any]:
    merged = dict(total)
    for key, value in more.items():
        if isinstance(value, (int, float)) and isinstance(merged.get(key), (int, float)):
            merged[key] = merged[key] + value
        else:
            merged[key] = value
    return merged


async def run_acp(
    install: ClioCoderInstall,
    home: InstanceHome,
    cwd: Path,
    prompts: Sequence[str],
    options: RunOptions,
    sink: EventSink | None = None,
    on_permission: PermissionHandler = reject_permissions,
) -> DelegationResult:
    """One ACP session: each prompt is a turn; a timeout or caller cancel sends ``session/cancel``."""
    args = ["acp", "--cwd", str(cwd)]
    if options.read_only:
        args.append("--read-only")
    if options.allow_tools is not None:
        args += ["--allow-tools", ",".join(options.allow_tools) or "none"]
    instance = await spawn_instance(install, args, cwd=cwd, home=home, stdin_pipe=True)
    result = DelegationResult(surface="acp", outcome=OUTCOME_FAILED)
    result.confinement = instance.confinement
    state = _AcpState(result, sink)
    stderr = bytearray()
    stderr_task = asyncio.create_task(_drain_stderr(instance, stderr))
    conn = AcpConnection(
        instance, on_notification=state.on_notification, on_permission=on_permission
    )
    try:
        await conn.start()
        session = await conn.request("session/new", {"cwd": str(cwd), "mcpServers": []})
        result.session_id = session["sessionId"]
        _emit(sink, {"kind": "session", "sessionId": result.session_id})
        result.model = await _configure_session(conn, session, options)
        await asyncio.wait_for(_prompt_all(conn, state, prompts), options.timeout)
    except TimeoutError:
        await _cancel(conn, result.session_id)
        result.outcome, result.outcome_detail = OUTCOME_TIMED_OUT, "parent_timeout"
    except asyncio.CancelledError:
        await _cancel(conn, result.session_id)
        raise
    except ClioCoderError as exc:
        result.outcome, result.error = OUTCOME_FAILED, exc.to_dict()
    finally:
        if result.session_id is not None and instance.process.returncode is None:
            with contextlib.suppress(ClioCoderError, TimeoutError):
                await asyncio.wait_for(
                    conn.request("session/close", {"sessionId": result.session_id}),
                    _CANCEL_SETTLE_SECONDS,
                )
        await conn.close()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(stderr_task, 5.0)
        result.stderr_tail = stderr.decode("utf-8", "replace")
        result.exit_code = instance.process.returncode
        result.tools = list(state.tools.values())
    return result


async def _configure_session(
    conn: AcpConnection, session: dict[str, Any], options: RunOptions
) -> str | None:
    session_id = session["sessionId"]
    if options.target:
        await conn.request(
            "session/set_config_option",
            {"sessionId": session_id, "configId": "target", "value": options.target},
        )
    if options.model:
        await conn.request(
            "session/set_config_option",
            {"sessionId": session_id, "configId": "model", "value": options.model},
        )
    current_mode = (session.get("modes") or {}).get("currentModeId")
    if options.autonomy != current_mode:
        await conn.request(
            "session/set_mode", {"sessionId": session_id, "modeId": options.autonomy}
        )
    if options.model:
        return options.model
    for option in session.get("configOptions") or []:
        if option.get("id") == "model":
            return option.get("currentValue")
    return None


async def _prompt_all(conn: AcpConnection, state: _AcpState, prompts: Sequence[str]) -> None:
    result = state.result
    for prompt in prompts:
        state.text.clear()
        reply = await conn.request(
            "session/prompt",
            {"sessionId": result.session_id, "prompt": [{"type": "text", "text": prompt}]},
        )
        stop_reason = str(reply.get("stopReason"))
        result.outcome = _STOP_REASON_OUTCOMES.get(stop_reason, OUTCOME_FAILED)
        result.outcome_detail = stop_reason
        result.text = "".join(state.text)
        meta = reply.get("_meta") or {}
        result.usage = _add_usage(result.usage, summarize_usage(meta.get("clio-coder/usage")))
        if result.outcome != OUTCOME_SUCCEEDED:
            return


async def _cancel(conn: AcpConnection, session_id: str | None) -> None:
    if session_id is None:
        return
    # ``session/close`` in the caller's cleanup then waits for the cancelled prompt to settle.
    with contextlib.suppress(ClioCoderError, ConnectionError):
        await conn.notify("session/cancel", {"sessionId": session_id})
