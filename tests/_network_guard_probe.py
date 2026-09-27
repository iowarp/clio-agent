"""Inner tests for ``test_network_guard.py`` (never collected by a normal run)."""

from __future__ import annotations

import socket

import pytest


def test_swallows_a_blocked_lookup() -> None:
    # Code under test that treats any network error as "offline" and carries on.
    try:
        socket.getaddrinfo("example.com", 443)
    except OSError:
        pass


@pytest.mark.integration
def test_marked_integration_is_exempt() -> None:
    from tests import _network_guard

    assert _network_guard._active is False  # checked directly: no real lookup needed
