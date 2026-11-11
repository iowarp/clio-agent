"""Schema-representable SSH results for the normal CLIO tool catalog."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, WithJsonSchema
from typing_extensions import NotRequired, TypedDict

from clio_agent.gact.infrastructure.models import SshRoute


class SshOutputSpill(TypedDict):
    """Existing shell spill receipt; a failed save never claims a complete file."""

    status: Literal["spilled", "spill_failed"]
    reason: str
    total_bytes: int
    total_lines: int
    head_lines: int
    tail: str
    tail_lines: int
    path: NotRequired[str]
    relative_path: NotRequired[str]
    error: NotRequired[str]
    note: NotRequired[str]


class SshCommandEchoOmitted(TypedDict):
    """A large command remains in the call input instead of being echoed twice."""

    reason: str
    chars: int


class SshCommandResult(TypedDict):
    """Actual remote execution facts, returned as a dictionary without rewriting output."""

    ssh_route: Annotated[
        dict[str, Any],
        WithJsonSchema(SshRoute.model_json_schema()),
        Field(description="Normalized shared SshRoute metadata."),
    ]
    command: NotRequired[str]
    command_omitted: NotRequired[SshCommandEchoOmitted]
    exit_code: Annotated[int | None, Field(description="Remote exit status; null if unavailable.")]
    status: Annotated[
        Literal["completed", "failed"],
        Field(description="Completed only when the command ended and its exit status is zero."),
    ]
    stop_reason: Literal["completed", "timeout", "cancelled", "exit_status_unavailable"]
    timeout_s: float
    stdout: Annotated[str, Field(description="Actual stdout, or its head when truncated.")]
    stderr: Annotated[str, Field(description="Actual stderr, or its head when truncated.")]
    stdout_truncated: bool
    stderr_truncated: bool
    stdout_spill: NotRequired[SshOutputSpill]
    stderr_spill: NotRequired[SshOutputSpill]
    remote_process_state: NotRequired[
        Annotated[
            str, Field(description="Remote descendants are unconfirmed after channel closure.")
        ]
    ]
