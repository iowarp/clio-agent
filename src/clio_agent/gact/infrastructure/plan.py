"""The compiled lifecycle plan a driver hands the infrastructure runtime."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from clio_agent.gact.infrastructure.clio_agent_deploy import ClaimResult
from clio_agent.gact.infrastructure.models import CommandSpec
from clio_agent.gact.infrastructure.remote_lifecycle import RemoteLaunch
from clio_agent.gact.infrastructure.resource_ledger import StepRecorder


@dataclass(frozen=True)
class Readiness:
    """How the runtime waits for a started server to answer.

    The runtime polls ``health`` (its output contains ``ready`` once the
    server answers) and, between polls, ``alive`` (its output contains
    ``running`` while the server process lives). There is no fixed deadline:
    a server that exits is reported at once with ``logs``; one that is still
    loading is waited for, and the person's cancel is what stops the wait.
    """

    health: CommandSpec
    alive: CommandSpec
    logs: CommandSpec
    label: str = "server"


@dataclass(frozen=True)
class DriverPlan:
    """Commands and endpoint metadata for one validated lifecycle action.

    Attributes:
        commands: Commands run in order on the target.
        connection_port: The target port the service is reached on.
        teardown: Undo what this plan started when it fails or is cancelled,
            given what its claim step found (see clio_agent_deploy).
        recorders: Per command index, what that step created on the target
            (see :mod:`clio_agent.gact.infrastructure.resource_ledger`).
        readiness: Wait for the started server to answer before succeeding.
        after_ready: Commands run once the server answers (a model pull).
        configuration: The configuration as resolved by the driver (for
            example with the negotiated container runtime filled in); the
            runtime persists this rather than the raw request.
    """

    commands: tuple[CommandSpec, ...]
    connection_port: int | None = None
    teardown: Callable[[ClaimResult], CommandSpec] | None = None
    recorders: Mapping[int, StepRecorder] = field(default_factory=dict)
    readiness: Readiness | None = None
    after_ready: tuple[CommandSpec, ...] = ()
    configuration: dict[str, str] | None = None
    remote_launch: RemoteLaunch | None = None
