"""Reach a declared loopback port inside a fenced child's network namespace (Linux).

The Linux fence runs each confined child in its own network namespace, so a port a fenced
MCP server binds on ``127.0.0.1`` belongs to that namespace's loopback, not the host's. CLIO
reaches such a port (an MCP App's declared service) by dialing it from inside the namespace:
a short helper joins the user namespace that owns the network namespace (this user created
it, so no privilege is needed), then the network namespace, connects to
``127.0.0.1:<port>`` and passes the connected socket back over a socket pair.

Nothing listens anywhere: the helper dials only the one port it is given, and the socket it
returns is the only way in. The namespace is one of CLIO's own descendants': the one whose
processes carry the serving child's :data:`FENCE_CHILD_VARIABLE` and that listens on the
port, or, when the serving child is not known, the only fenced namespace listening on it.
"""

from __future__ import annotations

import errno
import logging
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Names a fenced child's egress channel in its environment, so its namespace can be found.
FENCE_CHILD_VARIABLE = "CLIO_FENCE_CHILD"

_HELPER_EXIT_S = 5.0

# Joins the namespace's owning user namespace (NS_GET_USERNS) and the namespace, then dials.
_HELPER = r"""
import errno, fcntl, os, socket, sys
pid, inode, port, fd = (int(value) for value in sys.argv[1:5])
back = socket.socket(fileno=fd)
try:
    net = os.open(f"/proc/{pid}/ns/net", os.O_RDONLY)
    if os.fstat(net).st_ino != inode:
        raise OSError(errno.ESRCH, "the namespace is gone")
    owner = fcntl.ioctl(net, 0xB701)
    if os.fstat(owner).st_ino != os.stat("/proc/self/ns/user").st_ino:
        os.setns(owner, os.CLONE_NEWUSER)
    os.setns(net, os.CLONE_NEWNET)
    conn = socket.create_connection(("127.0.0.1", port), timeout=float(sys.argv[5]))
except OSError as exc:
    back.sendall(b"E%d" % (exc.errno or errno.EIO))
else:
    socket.send_fds(back, [b"K"], [conn.fileno()])
"""


@dataclass(frozen=True)
class FencedNamespace:
    """A fenced child's network namespace: one process in it, and the namespace's inode."""

    pid: int
    inode: int


def fenced_services_possible() -> bool:
    """Whether a launched server can be in a network namespace of its own (an active Linux fence)."""

    if not sys.platform.startswith("linux"):
        return False
    from clio_agent.runtime.sandbox import fence_enforced  # noqa: PLC0415 - import cycle

    return fence_enforced()


def _net_inode(pid: int | str) -> int | None:
    try:
        return os.stat(f"/proc/{pid}/ns/net").st_ino
    except OSError:
        return None


def _carries(pid: int, child_id: str) -> bool:
    try:
        environ = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return False
    return f"{FENCE_CHILD_VARIABLE}={child_id}".encode() in environ.split(b"\0")


def _is_loopback(address: str) -> bool:
    """Whether a ``/proc/net/tcp{,6}`` local address is loopback or any-address."""

    address = address.upper()
    if len(address) == 8:  # IPv4, little-endian: 127.x.x.x ends in 7F
        return address.endswith("7F") or address == "00000000"
    ipv4_mapped = address.startswith("0000000000000000FFFF0000")
    return address in {"0" * 32, "0" * 24 + "01000000"} or (
        ipv4_mapped and _is_loopback(address[24:])
    )


def listening_ports(table: str) -> set[int]:
    """The loopback/any-address ports in LISTEN state in a ``/proc/net/tcp{,6}`` table."""

    ports: set[int] = set()
    for line in table.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[3] != "0A":
            continue
        address, _, port = fields[1].partition(":")
        try:
            if _is_loopback(address):
                ports.add(int(port, 16))
        except ValueError:
            continue
    return ports


def _listens(pid: int, port: int) -> bool:
    for name in ("tcp", "tcp6"):
        try:
            table = Path(f"/proc/{pid}/net/{name}").read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        if port in listening_ports(table):
            return True
    return False


def _fenced_namespaces() -> dict[int, list[int]]:
    """CLIO's descendants outside its own network namespace, by namespace inode."""

    import psutil  # noqa: PLC0415

    own = _net_inode("self")
    try:
        descendants = psutil.Process().children(recursive=True)
    except psutil.Error:
        return {}
    found: dict[int, list[int]] = {}
    for process in descendants:
        inode = _net_inode(process.pid)
        if inode is not None and inode != own:
            found.setdefault(inode, []).append(process.pid)
    return found


def find_fenced_namespace(port: int, child_id: str = "") -> FencedNamespace | None:
    """The fenced namespace listening on loopback ``port``, or ``None``.

    With ``child_id`` (the serving child's egress channel), only that child's namespace is
    considered when its processes carry the marker. More than one candidate is ambiguous and
    returns ``None``, so a token is never sent to a namespace that may not be the server's.
    """

    if not sys.platform.startswith("linux"):
        return None
    namespaces = _fenced_namespaces()
    if child_id:
        marked = {
            inode: pids
            for inode, pids in namespaces.items()
            if any(_carries(pid, child_id) for pid in pids)
        }
        namespaces = marked or namespaces
    listening = [
        FencedNamespace(pids[0], inode)
        for inode, pids in namespaces.items()
        if _listens(pids[0], port)
    ]
    if len(listening) > 1:
        logger.warning(
            "fenced service namespace ambiguous reason=fenced_service_ambiguous port=%s "
            "namespaces=%s",
            port,
            [target.inode for target in listening],
        )
        return None
    return listening[0] if listening else None


def connect_in_namespace(
    target: FencedNamespace, port: int, timeout: float = 10.0
) -> socket.socket:
    """Connect to ``127.0.0.1:port`` inside ``target`` and return the connected socket.

    Raises:
        OSError: The namespace is gone, could not be joined, or nothing accepted.
    """

    ours, theirs = socket.socketpair()
    with ours:
        try:
            helper = subprocess.Popen(  # noqa: S603 - CLIO's own interpreter and code
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    _HELPER,
                    str(target.pid),
                    str(target.inode),
                    str(port),
                    str(theirs.fileno()),
                    str(timeout),
                ],
                pass_fds=(theirs.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        finally:
            theirs.close()
        ours.settimeout(timeout + _HELPER_EXIT_S)
        try:
            message, fds, _flags, _address = socket.recv_fds(ours, 16, 1)
        finally:
            try:
                helper.wait(timeout=_HELPER_EXIT_S)
            except subprocess.TimeoutExpired:
                helper.kill()
                helper.wait()
    if message == b"K" and fds:
        return socket.socket(fileno=fds[0])
    for fd in fds:
        os.close(fd)
    code = int(message[1:]) if message[:1] == b"E" and message[1:].isdigit() else errno.EIO
    raise OSError(code, f"fenced service dial failed: {os.strerror(code)}")


__all__ = [
    "FENCE_CHILD_VARIABLE",
    "FencedNamespace",
    "connect_in_namespace",
    "fenced_services_possible",
    "find_fenced_namespace",
    "listening_ports",
]
