"""Container runtime negotiation: probe what the target has, pick one, or say why not."""

from __future__ import annotations

import pytest

from clio_agent.gact.infrastructure.container_runtime import (
    RuntimeUnavailableError,
    negotiate_runtime,
    usable_runtimes,
)
from clio_agent.gact.infrastructure.models import ContainerRuntimeFact
from clio_agent.gact.infrastructure.runtime_probe import parse_runtime_lines

# Verbatim probe output from ares (2026-09-27): the login node, and a Slurm
# compute node where rootless Podman has no /run/user directory.
ARES_LOGIN = """Linux|x86_64|none|1|1|1
rt|docker|1|1|29.1.3|
rt|podman|1|1|3.4.4|
rt|apptainer|0|0||
id|1008|65534|/home/jcernudagarcia
"""
ARES_COMPUTE = """Linux|x86_64|none|1|1|0\r
rt|docker|1|1|29.1.3|\r
rt|podman|1|0||time="2026-09-27T01:13:17-05:00" level=error msg="stat /run/user/1008: no such file or directory"\r
rt|apptainer|0|0||\r
id|1008|65534|/home/jcernudagarcia\r
"""


def test_probe_lines_parse_into_typed_runtime_facts_and_identity() -> None:
    runtimes, identity, home = parse_runtime_lines(ARES_LOGIN)

    assert [(fact.name, fact.installed, fact.usable) for fact in runtimes] == [
        ("docker", True, True),
        ("podman", True, True),
        ("apptainer", False, False),
    ]
    assert runtimes[0].version == "29.1.3"
    assert runtimes[2].reason == "not_installed"
    assert (identity.uid, identity.gid, identity.known) == (1008, 65534, True)
    assert home == "/home/jcernudagarcia"


def test_an_unusable_runtime_keeps_the_runtimes_own_words_as_its_reason() -> None:
    runtimes, _, home = parse_runtime_lines(ARES_COMPUTE)

    podman = next(fact for fact in runtimes if fact.name == "podman")
    assert podman.installed and not podman.usable
    assert podman.reason == "unusable"
    assert "stat /run/user/1008: no such file or directory" in podman.detail
    assert "cannot run containers" in podman.explanation()
    assert home == "/home/jcernudagarcia"


def test_a_probe_without_runtime_lines_reports_no_runtimes() -> None:
    runtimes, identity, home = parse_runtime_lines("linux|x86_64|none|1|1|1\n")

    assert runtimes == []
    assert not identity.known
    assert home == ""


def _facts(**usable: bool) -> list[ContainerRuntimeFact]:
    return [
        ContainerRuntimeFact(
            name=name,  # type: ignore[arg-type]
            installed=name in usable,
            usable=usable.get(name, False),
            reason=None
            if usable.get(name)
            else ("unusable" if name in usable else "not_installed"),
            detail="" if usable.get(name, True) else "engine unreachable",
        )
        for name in ("docker", "podman", "apptainer")
    ]


def test_automatic_negotiation_prefers_docker_then_podman_then_apptainer() -> None:
    assert negotiate_runtime(_facts(docker=True, podman=True)).name == "docker"
    assert negotiate_runtime(_facts(docker=False, podman=True)).name == "podman"
    assert negotiate_runtime(_facts(apptainer=True)).name == "apptainer"
    assert usable_runtimes(_facts(apptainer=True, podman=True)) == ["podman", "apptainer"]


def test_an_explicit_usable_choice_wins_over_preference() -> None:
    assert negotiate_runtime(_facts(docker=True, podman=True), "podman").name == "podman"
    assert negotiate_runtime(_facts(docker=True, podman=True), " Podman ").name == "podman"


def test_an_explicit_unusable_choice_is_refused_never_swapped() -> None:
    with pytest.raises(RuntimeUnavailableError) as raised:
        negotiate_runtime(_facts(docker=True, podman=False), "podman")

    assert raised.value.reason == "runtime_not_usable"
    assert "engine unreachable" in str(raised.value)


def test_a_missing_runtime_choice_says_it_is_not_installed() -> None:
    with pytest.raises(RuntimeUnavailableError) as raised:
        negotiate_runtime(_facts(docker=True), "apptainer")

    assert raised.value.reason == "runtime_not_usable"
    assert "Apptainer is not installed" in str(raised.value)


def test_no_usable_runtime_lists_every_runtimes_reason() -> None:
    with pytest.raises(RuntimeUnavailableError) as raised:
        negotiate_runtime(_facts(docker=False, podman=False))

    message = str(raised.value)
    assert raised.value.reason == "no_usable_runtime"
    assert "Docker is installed but cannot run containers" in message
    assert "Podman is installed but cannot run containers" in message
    assert "Apptainer is not installed" in message


def test_an_unknown_runtime_name_is_a_typed_refusal() -> None:
    with pytest.raises(RuntimeUnavailableError) as raised:
        negotiate_runtime(_facts(docker=True), "singularity")

    assert raised.value.reason == "runtime_unknown"


def test_a_stored_runtime_name_is_parsed_once_into_the_typed_name() -> None:
    from clio_agent.gact.infrastructure.container_runtime import parse_runtime_name

    assert parse_runtime_name(" Podman ") == "podman"
    with pytest.raises(RuntimeUnavailableError) as raised:
        parse_runtime_name("singularity")
    assert raised.value.reason == "runtime_unknown"

