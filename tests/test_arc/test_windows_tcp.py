"""Windows listener discovery is real OS state, with bounded fail-closed decoding."""

from __future__ import annotations

import ctypes
import socket
import struct
from typing import Any

import pytest

from clio_agent.arc import windows_tcp


@pytest.mark.parametrize(
    ("state", "address", "port", "present"),
    [
        (2, 0x0100007F, 9413, True),
        (2, 0, 9413, True),
        (5, 0, 9413, False),
        (2, 0x0100A8C0, 9413, False),
        (2, 0, 9414, False),
    ],
)
def test_requires_matching_listening_loopback_endpoint(
    state: int, address: int, port: int, present: bool
) -> None:
    table = struct.pack("<7I", 1, state, address, socket.htons(port), 0, 0, 123)
    assert windows_tcp._table_has_loopback_listener(table, 9413) is present


@pytest.mark.parametrize(
    "table", [b"", b"\x01\0", struct.pack("<I", 1), struct.pack("<I", 0xFFFFFFFF)]
)
def test_truncated_table_is_not_healthy_or_absent(table: bytes) -> None:
    with pytest.raises(OSError):
        windows_tcp._table_has_loopback_listener(table, 9413)


def test_empty_table_proves_no_listener() -> None:
    assert not windows_tcp._table_has_loopback_listener(struct.pack("<I", 0), 9413)


def test_native_query_resizes_without_opening_a_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    table = struct.pack("<7I", 1, 2, 0, socket.htons(9413), 0, 0, 123)
    calls: list[bool] = []

    def query(buffer: Any, size: Any, ordered: bool, family: int, kind: int, reserved: int) -> int:
        assert (ordered, family, kind, reserved) == (False, socket.AF_INET, 3, 0)
        calls.append(buffer is None)
        ctypes.cast(size, ctypes.POINTER(ctypes.c_uint32)).contents.value = len(table)
        if buffer is None:
            return 122
        ctypes.memmove(buffer, table, len(table))
        return 0

    monkeypatch.setattr(windows_tcp, "_table_function", lambda: query)
    assert windows_tcp.loopback_listener_present(9413)
    assert calls == [True, False]


@pytest.mark.parametrize("failure", ["denied", "oversized", "changing"])
def test_query_failure_is_never_reclassified_as_listener_absence(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    def query(buffer: Any, size: Any, *args: Any) -> int:
        ctypes.cast(size, ctypes.POINTER(ctypes.c_uint32)).contents.value = (
            (1 << 30) if failure == "oversized" else 32
        )
        return 5 if failure == "denied" else 122

    monkeypatch.setattr(windows_tcp, "_table_function", lambda: query)
    with pytest.raises(OSError):
        windows_tcp.loopback_listener_present(9413)
