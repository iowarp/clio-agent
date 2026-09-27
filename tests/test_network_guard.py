"""Unit tests never reach a non-loopback host (tests/_network_guard.py)."""

from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from tests import _network_guard
from tests._inner_suite import REPO_ROOT, inner_runtime_parent, run_inner_pytest
from tests._network_guard import OutboundNetworkBlocked

PROBE = REPO_ROOT / "tests" / "_network_guard_probe.py"


@contextmanager
def _collect_violations() -> Iterator[list[str]]:
    """Take this test's recorded violations (so the guard does not fail it), then resume."""
    found: list[str] = []
    try:
        yield found
    finally:
        found.extend(_network_guard.end())
        _network_guard.begin()


def test_a_lookup_of_a_remote_name_is_blocked_and_recorded() -> None:
    with _collect_violations() as found, pytest.raises(OutboundNetworkBlocked):
        socket.getaddrinfo("example.com", 443)
    assert len(found) == 1 and "example.com" in found[0]
    assert "test_network_guard.py" in found[0]  # the record names where it came from


def test_a_connect_to_a_remote_address_is_blocked() -> None:
    sock = socket.socket()
    try:
        with _collect_violations() as found, pytest.raises(OutboundNetworkBlocked):
            sock.connect(("192.0.2.1", 443))  # TEST-NET-1: never routable
    finally:
        sock.close()
    assert found and "192.0.2.1" in found[0]


def test_blocked_is_an_oserror_so_code_takes_its_offline_path() -> None:
    assert issubclass(OutboundNetworkBlocked, OSError)


def test_loopback_stays_open() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    accepted: list[socket.socket] = []
    thread = threading.Thread(target=lambda: accepted.append(server.accept()[0]), daemon=True)
    thread.start()
    try:
        assert socket.getaddrinfo("localhost", port)
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
        thread.join(5)
    finally:
        for sock in accepted:
            sock.close()
        server.close()


@pytest.mark.timeout(240)  # a nested pytest session with its own private daemon
def test_a_swallowed_lookup_still_fails_the_test_and_integration_is_exempt() -> None:
    with inner_runtime_parent() as runtime_parent:
        completed = run_inner_pytest(
            runtime_parent,
            [str(PROBE), "-p", "no:xdist", "-rA", "-m", ""],
            backstop_s=180,
        )
    output = completed.stdout + completed.stderr
    # Reported at the test's teardown (the fixture that owns the guard), so it shows as
    # an ERROR of that test: red either way.
    assert "ERROR tests/_network_guard_probe.py::test_swallows_a_blocked_lookup" in output, output
    assert completed.returncode == 1, output
    assert "reached the network" in output and "example.com" in output, output
    assert "PASSED tests/_network_guard_probe.py::test_marked_integration_is_exempt" in output, (
        output
    )
