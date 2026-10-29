"""Inspect Windows' live IPv4 listeners without consuming an ephemeral TCP port."""

from __future__ import annotations

import ctypes
import errno
import socket
import struct
from functools import cache
from typing import Any, cast

_ROW = struct.Struct("<6I")  # MIB_TCPROW_OWNER_PID: six DWORDs on x64 and ARM64.
_MAX_TABLE_BYTES = 32 * 1024 * 1024


@cache
def _table_function() -> Any:
    # ctypes exposes this export only on Windows; non-Windows type stubs omit it.
    library: Any = cast(Any, ctypes).WinDLL("iphlpapi", use_last_error=True)
    function = library.GetExtendedTcpTable
    function.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_int32,
        ctypes.c_uint32,
        ctypes.c_int32,
        ctypes.c_uint32,
    ]
    function.restype = ctypes.c_uint32
    return function


def _listener_table() -> bytes:
    function = _table_function()
    size = ctypes.c_uint32()
    buffer = None
    for _ in range(4):
        code = function(buffer, ctypes.byref(size), False, socket.AF_INET, 3, 0)
        # TCP_TABLE_OWNER_PID_LISTENER (3) includes only actual listening endpoints.
        if code == 0:
            if buffer is None:
                raise OSError(errno.EIO, "Windows returned no TCP listener table")
            return buffer.raw[: size.value]
        if code != 122:  # ERROR_INSUFFICIENT_BUFFER
            raise OSError(code, f"Windows TCP listener query failed ({code})")
        if size.value < 4 or size.value > _MAX_TABLE_BYTES:
            raise OSError(errno.EIO, "Windows TCP listener table size is invalid")
        buffer = ctypes.create_string_buffer(size.value)
    raise OSError(errno.EAGAIN, "Windows TCP listener table changed during inspection")


def _table_has_loopback_listener(table: bytes, port: int) -> bool:
    if len(table) < 4:
        raise OSError(errno.EIO, "Windows TCP listener table is truncated")
    count = struct.unpack_from("<I", table)[0]
    if count > (len(table) - 4) // _ROW.size:
        raise OSError(errno.EIO, "Windows TCP listener rows are truncated")
    for index in range(count):
        state, address, network_port, _, _, _pid = _ROW.unpack_from(table, 4 + index * _ROW.size)
        if (
            state == 2  # MIB_TCP_STATE_LISTEN
            and address in (0, 0x0100007F)  # wildcard or 127.0.0.1 in an in_addr DWORD
            and socket.ntohs(network_port & 0xFFFF) == port
        ):
            return True
    return False


def loopback_listener_present(port: int) -> bool:
    """Report OS listener presence; query failures remain errors, never guessed health.

    This replaces only the TCP connect used for listener discovery. The existing
    native RPC checks, daemon identity checks, TTL and stall quarantine still own
    actual client/daemon health.
    """
    return _table_has_loopback_listener(_listener_table(), port)
