"""Actual-model Question qualification through the product's normal HTTP routes."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

import _common as common
import requests

ANSWER = "Use the October owned dataset."


def disposable_question_paths(proof: Path) -> list[Path]:
    """List only children created by this fresh, isolated live-proof service."""
    return [
        proof / name
        for name in (
            "private-codex",
            "private-claude",
            "clio-home",
            "clio-kit-cache",
            "uv-cache",
            "web-state",
            "workspace",
        )
    ]


def cleanup_question_state(proof: Path) -> dict[str, bool]:
    """Remove disposable owned state after service settlement, retaining small evidence."""
    result = {}
    for path in disposable_question_paths(proof):
        if (
            path.parent != proof
            or path.resolve().parent != proof
            or path.is_symlink()
            or path.is_junction()
        ):
            raise ValueError(f"Unsafe owned cleanup target: {path}")
        if path.exists():
            shutil.rmtree(path)
        result[path.name] = not path.exists()
    return result


def capture_question_failure(proof: Path, sid: str) -> dict[str, Any]:
    """Retain the failed model's actual public state before disposable Core cleanup."""
    snapshot: dict[str, Any] = {"session_id": sid}
    ready = proof / "service-ready.json"
    if ready.is_file():
        base = json.loads(ready.read_text(encoding="utf-8"))["url"]
        for suffix in ("", "/messages", "/questions", "/async-processes"):
            try:
                response = requests.get(base + f"/v1/sessions/{sid}" + suffix, timeout=(3, 8))
                response.raise_for_status()
                snapshot[suffix or "session"] = response.json()
            except (requests.RequestException, ValueError) as exc:
                snapshot[suffix or "session"] = {"error": f"{type(exc).__name__}: {exc}"}
    common.dump_json(proof / f"failed-question-{sid}.json", snapshot)
    return snapshot


def question_mission(workspace: Path, mode: str) -> str:
    """Require another model action while the human answer remains unavailable."""
    beginning = (
        "Call ask_user(question='Which dataset should this qualification use?'). "
        "Wait for successful acceptance before making other calls. Omit optional arguments "
        "rather than inventing null values. Retain its Question handle. "
        "Immediately query_tasks(kind='Question'), then call "
        f"fs_read_file(path={(workspace / 'sentinel.txt').as_posix()!r}). "
        "Query_tasks(kind='Question') again to prove that the question is still pending "
        "after your independent read. If submission needed a retry, perform the independent "
        "file read again after you receive the accepted handle. "
    )
    if mode == "idle":
        return beginning + (
            "Finish this segment using assignment_disposition='yield'; report that the answer "
            "is pending. Do not wait or cancel. When its answer later wakes you, call "
            "get_task_result on that original handle and report the actual answer, then complete."
        )
    if mode == "stop":
        return beginning + (
            "Call wait_tasks on that handle with timeout_s=null; do not add polling waits. "
            "The operator will stop your turn during this unbounded wait. Leave the question "
            "open. Its later answer wakes you; call get_task_result on the original handle, "
            "report the actual answer and complete."
        )
    return beginning + (
        "Call wait_tasks on that handle with timeout_s=0.1. After it expires, call observe_tasks "
        "on the same handle, then wait_tasks with timeout_s=null. Finally call get_task_result "
        "on the original handle and report the actual answer. If the operator stops your turn, "
        "leave the question open. Its later answer wakes you; collect it and complete then."
    )


def answer_question_after_independent_work(
    call: Any, wid: str, sid: str, mode: str, *, max_elapsed: float, answer_delay_s: float = 0
) -> dict[str, Any]:
    """Supply an actual answer only after recorded independent model work or yielding."""
    checkpoint: dict[str, Any] = {}

    def ready() -> bool:
        messages = common.session_messages(call, sid)
        parts = [part for message in messages for part in message.get("parts", [])]
        reads = [
            part
            for part in parts
            if part.get("type") == "tool_result"
            and str(part.get("tool_name", "")).endswith("fs_read_file")
            and "independent action completed" in str(part)
            and not part.get("is_error")
        ]
        waits = [
            part
            for part in parts
            if part.get("type") == "tool_call" and part.get("tool_name") == "wait_tasks"
        ]
        questions = call("GET", f"/v1/sessions/{sid}/questions")["questions"]
        pending = [
            row for row in questions if row.get("task_handle") and row["status"] == "pending"
        ]
        returned = {part.get("call_id") for part in parts if part.get("type") == "tool_result"}
        live_unbounded = []
        for wait in waits:
            arguments = wait.get("input") or {}
            if not isinstance(arguments, dict):
                continue
            arguments = arguments.get("kwargs", arguments)
            if not isinstance(arguments, dict):
                continue
            selected = arguments.get("tasks", [])
            selected = (
                [selected]
                if isinstance(selected, str)
                else selected
                if isinstance(selected, list)
                else []
            )
            if (
                arguments.get("timeout_s") is None
                and wait.get("call_id") not in returned
                and pending
                and pending[0]["task_handle"] in selected
            ):
                live_unbounded.append(wait)
        waiting = bool(live_unbounded) and (mode == "stop" or len(waits) >= 2)
        if reads and pending and (mode == "idle" or waiting):
            checkpoint.update(question=pending[0], independent_read=reads[-1], wait_calls=waits)
            return True
        if reads and pending and mode != "idle":
            status = call("GET", f"/v1/sessions/{sid}")["status"]
            if status in {"idle", "completed", "error"}:
                raise RuntimeError(
                    f"Model ended before the required live unbounded {mode} wait; "
                    f"status={status}, recorded_waits={len(waits)}"
                )
        return False

    common.expanding_wait(
        ready, what="Question handle and independent model action", max_elapsed=max_elapsed
    )
    if "question" not in checkpoint:
        raise RuntimeError(f"Model did not reach the Question/{mode} checkpoint within its gate")
    if mode in {"idle", "stop"}:
        if mode == "stop":
            checkpoint["stop"] = call("POST", f"/v1/sessions/{sid}/cancel", {}, ok=(204,))
            checkpoint["before_answer_turn_status"] = common.expanding_wait(
                lambda: (
                    status
                    if (status := call("GET", f"/v1/sessions/{sid}")["status"])
                    in {"cancelled", "idle"}
                    else None
                ),
                what="stopped execution segment",
                max_elapsed=max_elapsed,
            )
        else:
            checkpoint["before_answer_turn_status"] = common.wait_turn(
                call, wid, sid, max_elapsed=max_elapsed
            )
        if checkpoint["before_answer_turn_status"] not in {"idle", "cancelled", "completed"}:
            raise RuntimeError("The prior execution segment did not settle before its answer")
    question = checkpoint["question"]
    checkpoint["tasks_before_answer"] = call("GET", f"/v1/sessions/{sid}/async-processes")
    if answer_delay_s > 0:
        not_before = time.monotonic() + answer_delay_s
        common.expanding_wait(
            lambda: time.monotonic() >= not_before,
            what="explicit live wait duration",
            max_elapsed=answer_delay_s + 10,
        )
    current = call("GET", f"/v1/sessions/{sid}/questions")["questions"]
    checkpoint["still_pending_before_answer"] = (
        next(row for row in current if row["id"] == question["id"])["status"] == "pending"
    )
    checkpoint["answer_submitted_at"] = time.time()
    checkpoint["answer_response"] = call(
        "POST", f"/v1/sessions/{sid}/questions/{question['id']}/answer", {"answer": ANSWER}
    )
    common.expanding_wait(
        lambda: any(
            part.get("type") == "tool_result"
            and part.get("tool_name") == "get_task_result"
            and ANSWER in str(part)
            for message in common.session_messages(call, sid)
            for part in message.get("parts", [])
        ),
        what="model collection of the actual Question answer",
        max_elapsed=max_elapsed,
    )
    return checkpoint
