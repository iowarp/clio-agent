"""An installed-but-unusable container runtime carries a typed failure the UI can say plainly."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from clio_agent.gact.infrastructure import runtime_probe
from clio_agent.gact.infrastructure.models import ContainerRuntimeFact
from clio_agent.gact.infrastructure.runtime_failure import classify_runtime_failure
from clio_agent.gact.infrastructure.runtime_probe import local_runtime_facts, parse_probe

# Verbatim: Docker Desktop installed but not running on Windows (owner's
# machine, CLIO 0.9.4.19, 2026-09-27).
DOCKER_DESKTOP_STOPPED = (
    'error during connect: Get "http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/v1.51/info": '
    "open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified."
)
LINUX_DAEMON_STOPPED = (
    "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
    "Is the docker daemon running?"
)
LINUX_SOCKET_DENIED = (
    "permission denied while trying to connect to the Docker daemon socket at "
    'unix:///var/run/docker.sock: Get "http://%2Fvar%2Frun%2Fdocker.sock/v1.47/info": '
    "dial unix /var/run/docker.sock: connect: permission denied"
)
PODMAN_MACHINE_STOPPED = (
    "Cannot connect to Podman. Please verify your connection to the Linux system using "
    "`podman system connection list`, or try `podman machine init` and `podman machine start`"
)
PODMAN_NO_RUNTIME_DIR = (
    'time="2026-09-27T01:13:17-05:00" level=error msg="stat /run/user/1008: '
    'no such file or directory"'
)


@pytest.mark.parametrize(
    ("name", "detail", "expected"),
    [
        ("docker", DOCKER_DESKTOP_STOPPED, "not_running"),
        ("docker", LINUX_DAEMON_STOPPED, "not_running"),
        ("docker", LINUX_SOCKET_DENIED, "permission_denied"),
        ("podman", PODMAN_MACHINE_STOPPED, "not_running"),
        # A setup problem, not a stopped engine: never guessed into a bucket.
        ("podman", PODMAN_NO_RUNTIME_DIR, "unknown"),
        ("apptainer", "", "unknown"),
    ],
)
def test_common_runtime_failures_are_classified(name: Any, detail: str, expected: str) -> None:
    assert classify_runtime_failure(name, detail) == expected


def test_a_health_command_that_never_answered_is_timed_out() -> None:
    assert classify_runtime_failure("docker", "", timed_out=True) == "timed_out"


class _Completed:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_local_probe_types_docker_desktop_not_running(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(argv: list[str], **_: object) -> _Completed:
        if argv[0] == "docker":
            return _Completed(1, stderr=DOCKER_DESKTOP_STOPPED + "\n")
        raise AssertionError(argv)

    monkeypatch.setattr(runtime_probe.subprocess, "run", _run)

    facts = local_runtime_facts(lambda name: name if name == "docker" else None)

    docker = facts[0]
    assert (docker.installed, docker.usable, docker.reason) == (True, False, "unusable")
    assert docker.failure == "not_running"
    assert "dockerDesktopLinuxEngine" in docker.detail, "raw text stays for a details view"
    assert docker.explanation() == "Docker is installed but not running."
    assert [(fact.name, fact.reason, fact.failure) for fact in facts[1:]] == [
        ("podman", "not_installed", None),
        ("apptainer", "not_installed", None),
    ]


def test_local_probe_types_a_hung_health_command(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(argv: list[str], **_: object) -> _Completed:
        raise subprocess.TimeoutExpired(argv, 8)

    monkeypatch.setattr(runtime_probe.subprocess, "run", _run)

    docker = local_runtime_facts(lambda name: name if name == "docker" else None)[0]

    assert docker.failure == "timed_out"
    assert docker.explanation() == "Docker is installed but did not respond."


def test_remote_probe_types_failures_from_the_runtime_line() -> None:
    stdout = (
        "Linux|x86_64|none|1|0|1\n"
        f"rt|docker|1|0||{LINUX_SOCKET_DENIED}\n"
        f"rt|podman|1|0||{PODMAN_NO_RUNTIME_DIR}\n"
        "rt|apptainer|1|1|apptainer version 1.3.4|\n"
        "id|1008|65534|node7|/home/me|\n"
    )

    runtimes, _, _, _ = parse_probe(stdout)

    assert [(fact.name, fact.failure) for fact in runtimes] == [
        ("docker", "permission_denied"),
        ("podman", "unknown"),
        ("apptainer", None),
    ]
    assert runtimes[0].explanation() == "Docker is installed but this account may not use it."


def test_an_unclassified_failure_still_explains_with_the_runtimes_words() -> None:
    fact = ContainerRuntimeFact(
        name="podman", installed=True, reason="unusable", failure="unknown", detail="boom"
    )

    assert fact.explanation() == "Podman is installed but cannot run containers: boom"
