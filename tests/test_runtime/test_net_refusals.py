"""How a kernel's refused egress is attributed to the script that held it."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from clio_agent.runtime import net_refusals


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    net_refusals.clear()
    yield
    net_refusals.clear()


def _refused_while_held(alongside: str) -> str:
    with net_refusals.watching():
        with net_refusals.own_channel("k1", label="the Python kernel") as egress:
            net_refusals.record("k1", "example.com", 443, "policy_denied", "deny example.com")
        return egress(alongside).get("network_refused", "")


def test_a_kernel_with_nothing_left_running_names_the_refusal_as_this_calls() -> None:
    text = _refused_while_held("")
    assert text.startswith("CLIO's sandbox refused network connections this call made.")
    assert "example.com:443" in text and "rule: deny example.com" in text


def test_a_kernel_an_earlier_script_left_running_in_names_what_else_could_have() -> None:
    text = _refused_while_held("1 thread that earlier scripts started was still running in it")
    assert "connections this call made" not in text
    assert "connections that the Python kernel made" in text
    assert "cannot tell whether this call made them (1 thread that earlier" in text
    assert "example.com:443" in text and "rule: deny example.com" in text
