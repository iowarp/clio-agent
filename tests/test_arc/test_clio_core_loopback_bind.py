"""CLIO's single-node clio-core runtime listens on loopback, never on every interface.

clio-core binds its RPC servers on ``0.0.0.0`` unless ``CLIO_BIND_ADDR`` names an
address, which exposed the desktop runtime's ports to the network.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc import clio_core_attach
from clio_agent.arc.clio_core_config import CORE_BIND_ADDR_ENV, pin_loopback_bind
from tests.test_arc.test_runtime_spawn_breakaway import _FakeProc, _spawn_with_popen


def test_pin_defaults_to_loopback() -> None:
    env: dict[str, str] = {"CLIO_BIND_ADDR": ""}
    pin_loopback_bind(env)
    assert env[CORE_BIND_ADDR_ENV] == "127.0.0.1"


def test_pin_respects_an_explicit_address() -> None:
    env = {CORE_BIND_ADDR_ENV: "10.0.0.5"}
    pin_loopback_bind(env)
    assert env[CORE_BIND_ADDR_ENV] == "10.0.0.5"


def test_spawned_daemon_binds_loopback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv(CORE_BIND_ADDR_ENV, raising=False)
    envs: list[dict[str, str]] = []

    def popen(_args: Any, **kwargs: Any) -> _FakeProc:
        envs.append(kwargs["env"])
        return _FakeProc()

    _spawn_with_popen(monkeypatch, tmp_path, popen)

    assert envs and envs[0][CORE_BIND_ADDR_ENV] == "127.0.0.1"


def test_attaching_client_leaves_the_server_environment_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(CORE_BIND_ADDR_ENV, raising=False)
    monkeypatch.delenv("CLIO_SERVER_CONF", raising=False)

    clio_core_attach.export_client_config("/tmp/cte.yaml")

    # The bind is scoped to clio-core's children and the native init, never this
    # server's environment (agent tools and every other child would inherit it).
    assert CORE_BIND_ADDR_ENV not in os.environ
