"""A native shell call's human view comes from the reply inside its task envelope.

The shapes below are the ones recorded live (2026-11-05, ``cp`` denied by the Landlock
fence, then a successful ``ls``): a foreground native shell call settles as
``{"handle", "status", "pending": False, "result": <recorded tasks/get reply>}`` and the
shell's own reply (``exit_code``/``stdout``/``stderr``/``boundary``) is that reply's
``result``. Reading the envelope as if it were the reply showed no output, no exit code
and "status=failed"; the running task's status line was a raw terminal-chunk JSON.
"""

from __future__ import annotations

import copy
import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

from fastmcp_tasks.client_models import ClientGetTaskResult

from clio_agent.arc.task_types import BackendReference, CoreReference, TaskOwner, TaskSnapshot
from clio_agent.gact.native_tools import NativeTools
from clio_agent.gact.presentation_observer import completed_presentation
from clio_agent.gact.tool_execution_projection import project_execution
from clio_agent.tools.result_errors import structured_tool_result_error
from clio_agent.tools.tool_presentation import present_mcp_result

COMMAND = "cp notes.txt /home/user/clio-outside-1905/notes.txt"
STDERR = (
    "cp: cannot create regular file '/home/user/clio-outside-1905/notes.txt': Permission denied\n"
)
CHUNK = json.dumps({"type": "clio.terminal.chunk", "stream": "stderr", "text": "cp: "})
BOUNDARY = {
    "kind": "write_denied",
    "confidence": "confirmed",
    "mechanism": "landlock",
    "paths": ["/home/user/clio-outside-1905/notes.txt"],
    "request_roots": ["/home/user/clio-outside-1905"],
    "message": "CLIO's sandbox blocked this write. /home/user/clio-outside-1905 is not a "
    "permitted folder.",
    "signature": "EACCES",
}


def shell_row(*, exit_code: int, stdout: str = "", stderr: str = "") -> dict[str, Any]:
    row: dict[str, Any] = {
        "cwd": "/work/sb7",
        "exit_code": exit_code,
        "timed_out": False,
        "timeout_s": 0.0,
        "stdout": stdout,
        "stdout_truncated": False,
        "stderr": stderr,
        "stderr_truncated": False,
    }
    if exit_code:
        row["boundary"] = BOUNDARY
    return row


def task_reply(row: dict[str, Any]) -> dict[str, Any]:
    """The recorded ``tasks/get`` reply: always ``completed``; the shell's row inside."""
    return {
        "meta": {"io.modelcontextprotocol/serverInfo": {"name": "CLIO shell", "version": "4"}},
        "task_id": "backend-task-1",
        "status": "completed",
        "created_at": "2026-11-05T22:32:36.883558+00:00",
        "last_updated_at": "2026-11-05T22:32:37.149718+00:00",
        "ttl_ms": 900000,
        "status_message": None,
        "poll_interval_ms": 200,
        "result_type": "complete",
        "result": {
            "content": [{"type": "text", "text": json.dumps(row)}],
            "structuredContent": row,
            "isError": False,
            "resultType": "complete",
        },
        "error": None,
        "input_requests": None,
    }


def observed(row: dict[str, Any]) -> dict[str, Any]:
    """The observer copy of a settled foreground call: the envelope as structuredContent."""
    status = "failed" if row["exit_code"] else "completed"
    envelope = {
        "handle": "task_607802e927f64b7d8db33687c0fb5eb9",
        "status": status,
        "pending": False,
        "result": task_reply(row),
    }
    return {"content": [], "structuredContent": envelope}


FAILED = shell_row(exit_code=1, stderr=STDERR)
SUCCEEDED = shell_row(exit_code=0, stdout="-rw-r--r-- 1 root root 31 notes.txt\nidentical\n")


def terminal(view: dict[str, Any]) -> dict[str, Any]:
    return next(block for block in view["blocks"] if block["type"] == "terminal")


def test_shell_presentation_reads_the_reply_inside_a_task_envelope() -> None:
    failed = present_mcp_result("shell_bash", {"command": COMMAND}, observed(FAILED))
    assert failed["status"] == "failed"
    assert terminal(failed)["command"] == COMMAND
    assert terminal(failed)["text"] == STDERR
    assert terminal(failed)["exit_code"] == 1
    assert terminal(failed)["detail"] == "Working directory: /work/sb7"

    succeeded = present_mcp_result("shell_bash", {"command": "ls"}, observed(SUCCEEDED))
    assert "status" not in succeeded
    assert terminal(succeeded)["text"] == SUCCEEDED["stdout"]
    assert terminal(succeeded)["exit_code"] == 0


def test_recorded_failure_shows_output_and_exit_code_not_status_failed() -> None:
    result = observed(FAILED)
    error = structured_tool_result_error(result)
    assert error == "status=failed"  # the envelope's verdict, which the view must not echo
    _result, view = completed_presentation(
        "shell_bash", {"command": COMMAND}, result, result["structuredContent"], "", error=error
    )
    assert view["status"] == "failed"
    assert terminal(view)["text"] == STDERR
    assert terminal(view)["exit_code"] == 1
    assert "status=failed" not in json.dumps(view)
    assert not any(block.get("id") == "execution-error" for block in view["blocks"])

    result = observed(SUCCEEDED)
    _result, view = completed_presentation(
        "shell_bash", {"command": "ls"}, result, result["structuredContent"], ""
    )
    assert terminal(view)["text"] == SUCCEEDED["stdout"]


def test_a_settled_reply_with_its_own_error_still_says_so() -> None:
    """Only a failure the presenter explained from the reply's facts drops the block."""
    result = observed(FAILED)
    reply = result["structuredContent"]["result"]["result"]
    reply["structuredContent"] = {"error": {"code": "bad", "message": "Readable reason"}}
    _result, view = completed_presentation(
        "shell_bash", {}, result, None, "", error=structured_tool_result_error(result)
    )
    assert view["blocks"][-1]["id"] == "execution-error"


def _history(reply: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(read_content=lambda reference: copy.deepcopy(reply))


def _task(status: str, message: str) -> TaskSnapshot:
    return TaskSnapshot(
        handle="task_607802e927f64b7d8db33687c0fb5eb9",
        kind="Shell",
        tool="bash",
        label="Command",
        owner=TaskOwner(human_id="human", session_id="sess_1", assignment_id="assignment_1"),
        invocation_id="call_1",
        created_at="2026-11-05T22:32:36+00:00",
        updated_at="2026-11-05T22:32:37+00:00",
        backend=BackendReference(owner="native", server="clio-shell", task_id="backend-task-1"),
        status=status,
        progress={"message": message},
        terminal_at="2026-11-05T22:32:37+00:00",
        result=CoreReference(session_id="sess_1", scope="task", content_id="c1", sha256="0" * 64),
    )


def test_execution_view_presents_the_native_shell_reply() -> None:
    tool = {"id": "call_1", "name": "shell_bash", "input": {"command": COMMAND}, "state": "failed"}
    entity, _view = project_execution(
        _history(task_reply(FAILED)), tool, _task("failed", "Shell output available")
    )
    view = entity["presentation"]
    assert view["status"] == "failed"
    assert terminal(view)["text"] == STDERR
    assert terminal(view)["exit_code"] == 1
    # No outline of the reply's fields, no "Execution status" diagnostic, no error string.
    assert [block["type"] for block in view["blocks"]] == ["terminal"]
    assert "error" not in entity
    # The sandbox diagnosis stays on the output the notice reads.
    assert entity["output"]["structuredContent"]["boundary"]["kind"] == "write_denied"

    tool = {"id": "call_1", "name": "shell_bash", "input": {"command": "ls"}, "state": "running"}
    entity, _view = project_execution(_history(task_reply(SUCCEEDED)), tool, _task("completed", ""))
    assert entity["presentation"]["status"] == "succeeded"
    assert terminal(entity["presentation"])["text"] == SUCCEEDED["stdout"]


def test_a_terminal_chunk_status_message_is_not_the_task_status_line() -> None:
    """The SDK status message repeats each output chunk; the task keeps its own line."""
    current = _task("running", "Shell output available").model_copy(
        update={"terminal_at": None, "result": None, "raw_status": "working"}
    )
    recorded: list[TaskSnapshot] = []
    owner = SimpleNamespace(
        history=SimpleNamespace(
            serialized=nullcontext,
            native_attempts=lambda handle: [],
            record_snapshot=lambda snapshot, observation_id: recorded.append(snapshot),
        ),
        _current=lambda handle: current,
        _settle=lambda snapshot, accepted: None,
    )
    accepted = SimpleNamespace(submission=SimpleNamespace(handle=current.handle))
    working = ClientGetTaskResult.model_validate(
        {**task_reply(FAILED), "status": "working", "result": None, "status_message": CHUNK}
    )
    NativeTools._record_reply(owner, accepted, working)  # type: ignore[arg-type]
    assert recorded and recorded[-1].progress.message == "Shell output available"

    words = working.model_copy(update={"status_message": "Waiting for the lock"})
    NativeTools._record_reply(owner, accepted, words)  # type: ignore[arg-type]
    assert recorded[-1].progress.message == "Waiting for the lock"
