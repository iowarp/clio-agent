"""Pooled lm15 connections stay reusable when socket descriptors exceed FD_SETSIZE.

lm15's own probe used ``select.select``, which raises for descriptors >= 1024; lm15 read
that as "stale" and opened a new connection for every request in any process holding
more than ~1024 descriptors (a long-lived server, a late full-suite worker).
"""

from __future__ import annotations

import os
import socket
import sys
from collections.abc import Iterator
from importlib import import_module
from typing import Any

import pytest
from dspy.lm15 import Message, Request

from clio_agent.config import LMProviderConfig
from clio_agent.lm import pool_liveness
from clio_agent.lm.engines.lm_loop import run_on_lm_loop
from clio_agent.lm.factory import create_lm

_ABOVE = 1100  # comfortably past FD_SETSIZE (1024)


@pytest.fixture
def high_descriptors() -> Iterator[None]:
    """Hold descriptors so sockets created by the test are numbered above FD_SETSIZE."""
    if sys.platform == "win32":
        pytest.skip("Windows select limits socket count, not descriptor value")
    import resource  # noqa: PLC0415 - POSIX only

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard < _ABOVE + 64:
        pytest.skip(f"descriptor hard limit {hard} cannot exceed FD_SETSIZE")
    if soft != resource.RLIM_INFINITY and soft < _ABOVE + 64:
        resource.setrlimit(resource.RLIMIT_NOFILE, (_ABOVE + 64, hard))
    held: list[int] = []
    try:
        while not held or held[-1] < _ABOVE:
            held.append(os.open(os.devnull, os.O_RDONLY))
        yield
    finally:
        for fd in held:
            os.close(fd)
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_lm15_connections_use_the_descriptor_safe_probe() -> None:
    pool_liveness.install_pool_liveness()
    sync = import_module("dspy._vendor.lm15.transports._sync")
    asynchronous = import_module("dspy._vendor.lm15.transports._async")
    assert sync._SyncConnection.is_stale is pool_liveness._sync_is_stale
    assert asynchronous._AsyncConnection.is_stale is pool_liveness._async_is_stale


def test_idle_socket_above_fd_setsize_is_reusable_until_the_peer_acts(
    high_descriptors: None,
) -> None:
    left, right = socket.socketpair()
    try:
        assert left.fileno() >= 1024
        assert not pool_liveness.idle_socket_unusable(left, "sync")
        right.sendall(b"x")  # unexpected bytes on an idle keep-alive connection
        assert pool_liveness.idle_socket_unusable(left, "sync")
        left.recv(1)
        right.close()  # peer EOF
        assert pool_liveness.idle_socket_unusable(left, "sync")
    finally:
        left.close()
        right.close()
    assert pool_liveness.idle_socket_unusable(left, "sync")  # closed underneath the pool
    assert pool_liveness.idle_socket_unusable(None, "async")


def test_repeated_requests_reuse_one_connection_above_fd_setsize(
    endpoint: tuple[str, dict[str, Any]],
    high_descriptors: None,
) -> None:
    base, state = endpoint
    lm = create_lm(
        LMProviderConfig(
            provider="openai",
            provider_id="amsc_mag",
            model="test-goal",
            api_base=base,
            api_key="local-test-only",
        )
    )
    request = Request(model=lm.model, messages=(Message.user("work"),))
    try:
        for _ in range(5):
            run_on_lm_loop(lambda: lm.acall(request))
    finally:
        run_on_lm_loop(lm.aclose)
    assert state["calls"] == 5
    assert state["connections"] == 1
    assert state["max_open"] == 1


def test_a_failed_probe_refuses_reuse_with_a_typed_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "clio_agent.runtime.stream_audit.stream_audit",
        lambda stage, /, **fields: recorded.append((stage, fields)),
    )

    class Broken:
        def fileno(self) -> int:
            raise OSError("descriptor unavailable")

    assert pool_liveness.idle_socket_unusable(Broken(), "async")
    assert recorded == [
        (
            "lm.pool.liveness_probe_failed",
            {"transport": "async", "reason": "liveness_probe_failed", "error_type": "OSError"},
        )
    ]
