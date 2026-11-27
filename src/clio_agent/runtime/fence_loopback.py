"""Reach a fenced server's loopback listeners from outside Windows' native MXC fence.

Codex's MXC container lets a confined child dial out to the host's loopback, but drops
connections from the host to the container's own loopback listeners (Microsoft documents
host-to-container ingress as unresolved), and the two share one port space, so the host
cannot listen on the same port either. A fenced MCP server's sign-in page or port forward
on ``127.0.0.1:<port>`` would be unreachable from the desktop and from CLIO.

So on that fence, CLIO starts each stdio MCP server behind
:mod:`clio_agent.runtime.fence_loopback_agent`, which keeps one connection out to the
:class:`LoopbackPublisher` here. When the server writes a loopback address in its output, the
agent asks to publish that port. The publisher checks that the server's own launch listens on
it (a process in its tree, or in the AppContainer the fence started it in, which is how a
detached port forward whose launcher has exited stays attributed to it), opens a host listener
on a fresh loopback port, and the address is
rewritten to it. Each connection to that port is spliced to a connection the agent makes to
the server's listener from inside the container. CLIO itself reaches a declared MCP App
service the same way without a host listener (:func:`connect`). Only ports the server's
launch listens on are ever published or dialed, and only on loopback. Linux and macOS are
unchanged.
"""

from __future__ import annotations

import ipaddress
import logging
import secrets
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clio_agent.runtime.fence_loopback_agent import VARIABLE, splice

logger = logging.getLogger(__name__)

AGENT = Path(__file__).with_name("fence_loopback_agent.py")

_HELLO_S = 10.0
_LISTEN_WAIT_S = 2.0
_DIAL_S = 10.0
_QUERY_LIMITED = 0x1000
_TOKEN_QUERY = 0x0008
_TOKEN_APP_CONTAINER_SID = 31


def publishing_applies(state: Any) -> bool:
    """Whether ``state`` is Windows' native MXC fence, whose loopback listeners need this."""

    from clio_agent.runtime.sandbox import MECHANISM_CODEX  # noqa: PLC0415 - import cycle

    details = getattr(state, "details", {}) or {}
    return bool(
        sys.platform == "win32"
        and getattr(state, "active", False)
        and getattr(state, "mechanism", "") == MECHANISM_CODEX
        and details.get("codex", {}).get("implementation") == "mxc"
    )


@dataclass
class _Server:
    """One running agent: its control connection, and the ports published for its server."""

    token: str
    pid: int
    control: socket.socket
    child_id: str = ""
    #: The AppContainer SID of the server's launch ("" when it has none).
    container: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)
    published: dict[int, list[socket.socket]] = field(default_factory=dict)

    def send(self, line: str) -> None:
        with self.lock:
            self.control.sendall(line.encode("ascii") + b"\n")


def _is_loopback(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _tree(pid: int) -> set[int]:
    import psutil  # noqa: PLC0415

    try:
        return {pid, *(child.pid for child in psutil.Process(pid).children(recursive=True))}
    except psutil.Error:
        return set()


def _container_sid(pid: int) -> str:
    """The AppContainer SID on ``pid``'s token, or ``""`` (not one, gone, or not Windows).

    The MXC fence runs each launch in an AppContainer of its own, and Windows stamps that SID
    on every process started inside it, whatever becomes of its parent.
    """

    if sys.platform != "win32" or pid <= 0:
        return ""
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    process = kernel32.OpenProcess(_QUERY_LIMITED, False, pid)
    if not process:
        return ""
    token = wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
            return ""
        try:
            buffer = ctypes.create_string_buffer(256)
            size = wintypes.DWORD()
            if not advapi32.GetTokenInformation(
                token, _TOKEN_APP_CONTAINER_SID, buffer, len(buffer), ctypes.byref(size)
            ):
                return ""
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            text = wintypes.LPWSTR()
            if not sid or not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                return ""
            try:
                return text.value or ""
            finally:
                kernel32.LocalFree(text)
        finally:
            kernel32.CloseHandle(token)
    finally:
        kernel32.CloseHandle(process)


def _listener_address(pid: int, port: int, container: str = "") -> str | None:
    """The address to dial for loopback ``port`` when ``pid``'s launch listens on it.

    The launch is ``pid``'s process tree, and, given its ``container`` SID, every process in
    that AppContainer: a listener started by a launcher that has since exited (a detached port
    forward) is no longer in the tree but stays in the container.
    """

    import psutil  # noqa: PLC0415

    try:
        table = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError) as exc:
        logger.warning("fence loopback listeners unreadable reason=net_table_error error=%r", exc)
        return None
    tree = _tree(pid)
    for conn in table:
        if conn.status != psutil.CONN_LISTEN or not conn.laddr or conn.laddr.port != port:
            continue
        if not _is_loopback(conn.laddr.ip):
            continue
        if conn.pid in tree or (container and conn.pid and _container_sid(conn.pid) == container):
            if conn.pid not in tree:
                logger.info(
                    "fence loopback listener outside tree via=container port=%s pid=%s agent=%s",
                    port,
                    conn.pid,
                    pid,
                )
            ip = conn.laddr.ip
            if ipaddress.ip_address(ip.split("%", 1)[0]).is_unspecified:
                return "::1" if ":" in ip else "127.0.0.1"
            return ip
    return None


class LoopbackPublisher:
    """CLIO's end of every agent's control connection (one per CLIO process)."""

    def __init__(self) -> None:
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._listener.getsockname()[1]
        self._lock = threading.Lock()
        self._tokens: dict[str, str] = {}
        self._servers: dict[str, _Server] = {}
        self._dials: dict[str, tuple[str, threading.Event, list[socket.socket]]] = {}
        threading.Thread(target=self._accept, name="fence-loopback", daemon=True).start()

    def register(self) -> str:
        """A token for one server's spawn plan (every respawn of it reuses the token)."""

        token = secrets.token_hex(16)
        with self._lock:
            self._tokens[token] = ""
        return token

    def label(self, token: str, child_id: str) -> None:
        """Record the egress channel of the server ``token`` was issued to."""

        with self._lock:
            if token in self._tokens:
                self._tokens[token] = child_id

    def _accept(self) -> None:
        while True:
            try:
                conn, _peer = self._listener.accept()
            except OSError as exc:
                logger.warning("fence loopback publisher stopped reason=accept_error error=%r", exc)
                return
            threading.Thread(target=self._greet, args=(conn,), daemon=True).start()

    def _greet(self, conn: socket.socket) -> None:
        conn.settimeout(_HELLO_S)
        try:
            first = _read_line(conn)
        except OSError:
            conn.close()
            return
        parts = first.split()
        if len(parts) == 3 and parts[0] == "DATA":
            self._attach(conn, parts[1], parts[2])
        elif len(parts) == 3 and parts[0] == "HELLO" and parts[2].isdigit():
            self._serve(conn, parts[1], int(parts[2]))
        else:
            conn.close()

    def _serve(self, conn: socket.socket, token: str, pid: int) -> None:
        import psutil  # noqa: PLC0415

        with self._lock:
            child_id = self._tokens.get(token)
        try:
            ours = pid in {child.pid for child in psutil.Process().children(recursive=True)}
        except psutil.Error:
            ours = False
        if child_id is None or not ours:
            logger.warning("fence loopback agent refused reason=unknown_agent pid=%s", pid)
            conn.close()
            return
        conn.settimeout(None)
        server = _Server(
            token=token, pid=pid, control=conn, child_id=child_id, container=_container_sid(pid)
        )
        key = secrets.token_hex(8)
        with self._lock:
            self._servers[key] = server
        try:
            with conn.makefile("rb") as lines:
                for raw in lines:
                    self._command(server, raw.decode("ascii", "replace").split())
        except OSError as exc:
            logger.debug("fence loopback agent gone reason=control_closed error=%r", exc)
        finally:
            with self._lock:
                self._servers.pop(key, None)
            for listeners in list(server.published.values()):
                for listener in listeners:
                    listener.close()
            conn.close()

    def _command(self, server: _Server, parts: list[str]) -> None:
        if len(parts) == 2 and parts[0] == "PUB" and parts[1].isdigit():
            port = int(parts[1])
            published = self._publish(server, port)
            if published is None:
                server.send(f"NONE {port}")
            else:
                server.send(f"MAP {port} {published[0]} {int(published[1])}")
        elif len(parts) == 2 and parts[0] == "FAIL":
            self._finish(parts[1], None)

    def _publish(self, server: _Server, port: int) -> tuple[int, bool] | None:
        """``(host port, same port on [::1])`` for the server's ``port``, or ``None``."""

        if port in server.published:
            listeners = server.published[port]
            return listeners[0].getsockname()[1], len(listeners) > 1
        if self._owns(port):
            # Another server's published address (e.g. a pvserver handed to a client).
            return None
        deadline = time.monotonic() + _LISTEN_WAIT_S
        container = self._container(server)
        address = _listener_address(server.pid, port, container)
        while address is None and time.monotonic() < deadline:
            time.sleep(0.2)
            address = _listener_address(server.pid, port, container)
        if address is None:
            logger.info("fence loopback not published reason=not_listening port=%s", port)
            return None
        listeners = [socket.create_server(("127.0.0.1", 0))]
        try:
            listeners.append(socket.create_server(("::1", port), family=socket.AF_INET6))
        except OSError as exc:
            # The server holds [::1] or the IPv6 wildcard itself; its URLs take the host port.
            logger.info("fence loopback same port unavailable port=%s error=%r", port, exc)
        server.published[port] = listeners
        for listener in listeners:
            threading.Thread(
                target=self._relay, args=(server, listener, address, port), daemon=True
            ).start()
        outer = listeners[0].getsockname()[1]
        same = len(listeners) > 1
        logger.info(
            "fence loopback published port=%s host_port=%s same_port_v6=%s pid=%s",
            port,
            outer,
            same,
            server.pid,
        )
        return outer, same

    def _owns(self, port: int) -> bool:
        """Whether ``port`` is one of this publisher's own host listeners."""

        if port == self.port:
            return True
        with self._lock:
            servers = list(self._servers.values())
        for other in servers:
            for listeners in list(other.published.values()):
                for listener in listeners:
                    if listener.fileno() != -1 and listener.getsockname()[1] == port:
                        return True
        return False

    def _relay(self, server: _Server, listener: socket.socket, address: str, port: int) -> None:
        while True:
            try:
                conn, _peer = listener.accept()
            except OSError:
                return  # closed with the server's control connection
            threading.Thread(
                target=self._bridge, args=(server, conn, address, port), daemon=True
            ).start()

    def _bridge(self, server: _Server, conn: socket.socket, address: str, port: int) -> None:
        try:
            inner = self.dial(server, address, port, _DIAL_S)
        except OSError as exc:
            logger.warning(
                "fence loopback dial failed reason=dial_error port=%s error=%r", port, exc
            )
            conn.close()
            return
        splice(conn, inner)

    def dial(self, server: _Server, address: str, port: int, timeout: float) -> socket.socket:
        """A connection to ``address:port`` made by ``server``'s agent inside the container.

        Raises:
            OSError: The agent could not connect, or did not answer in time.
        """

        ident = secrets.token_hex(8)
        ready = threading.Event()
        slot: list[socket.socket] = []
        with self._lock:
            self._dials[ident] = (server.token, ready, slot)
        try:
            server.send(f"DIAL {ident} {address} {port}")
            if not ready.wait(timeout) or not slot:
                raise OSError(f"the fenced server did not connect to port {port}")
        finally:
            with self._lock:
                self._dials.pop(ident, None)
        sock = slot[0]
        sock.settimeout(None)
        return sock

    def _attach(self, conn: socket.socket, token: str, ident: str) -> None:
        with self._lock:
            waiting = self._dials.get(ident)
        if waiting is None or waiting[0] != token:
            conn.close()
            return
        self._finish(ident, conn)

    def _finish(self, ident: str, conn: socket.socket | None) -> None:
        with self._lock:
            waiting = self._dials.get(ident)
        if waiting is None:
            if conn is not None:
                conn.close()
            return
        if conn is not None:
            waiting[2].append(conn)
        waiting[1].set()

    def find(self, port: int, child_id: str = "") -> tuple[_Server, str] | None:
        """The running server (preferring ``child_id``'s) whose tree listens on ``port``."""

        with self._lock:
            servers = list(self._servers.values())
        marked = [server for server in servers if child_id and server.child_id == child_id]
        for server in marked or servers:
            address = _listener_address(server.pid, port, self._container(server))
            if address is not None:
                return server, address
        return None

    def _container(self, server: _Server) -> str:
        """``server``'s container SID, when no other running server shares it (else ``""``).

        A shared SID would no longer single out one server's launch, so its listeners are then
        found through its process tree only.
        """

        if not server.container:
            return ""
        with self._lock:
            others = [other for other in self._servers.values() if other is not server]
        if any(other.container == server.container for other in others):
            logger.warning(
                "fence loopback container shared reason=ambiguous_container pid=%s", server.pid
            )
            return ""
        return server.container


_PUBLISHER: LoopbackPublisher | None = None
_PUBLISHER_LOCK = threading.Lock()


def publisher() -> LoopbackPublisher:
    """This process's publisher, started on first use."""

    global _PUBLISHER
    with _PUBLISHER_LOCK:
        if _PUBLISHER is None:
            _PUBLISHER = LoopbackPublisher()
        return _PUBLISHER


def wrap_publishing(
    command: str, args: list[str], state: Any
) -> tuple[str, list[str], dict[str, str], str]:
    """``(command, args, env, token)``: ``command`` behind the agent on the MXC fence.

    Elsewhere the argv is unchanged, with no env and an empty token.
    """

    if not publishing_applies(state):
        return command, list(args), {}, ""
    try:
        owner = publisher()
    except OSError as exc:
        logger.warning("fence loopback publishing unavailable reason=listen_error error=%r", exc)
        return command, list(args), {}, ""
    token = owner.register()
    argv = ["-I", "-S", str(AGENT), "--", command, *args]
    return sys.executable, argv, {VARIABLE: f"{owner.port}:{token}"}, token


def publishing_active() -> bool:
    """Whether any fenced server here is behind the agent (so its listeners need it)."""

    return _PUBLISHER is not None


def find_server(port: int, child_id: str = "") -> int | None:
    """The agent pid of the fenced server listening on loopback ``port``, or ``None``."""

    if _PUBLISHER is None:
        return None
    found = _PUBLISHER.find(port, child_id)
    return found[0].pid if found else None


def connect(pid: int, port: int, timeout: float) -> socket.socket:
    """A connection to the fenced server ``pid``'s listener on loopback ``port``.

    Raises:
        OSError: No such server listens on ``port``, or its agent could not connect.
    """

    found = _PUBLISHER.find(port) if _PUBLISHER is not None else None
    if found is None or found[0].pid != pid:
        raise OSError(f"no fenced server listens on port {port}")
    server, address = found
    return _PUBLISHER.dial(server, address, port, timeout)


def _read_line(conn: socket.socket) -> str:
    data = b""
    while not data.endswith(b"\n") and len(data) < 256:
        chunk = conn.recv(1)
        if not chunk:
            break
        data += chunk
    return data.decode("ascii", "replace")


__all__ = [
    "AGENT",
    "LoopbackPublisher",
    "connect",
    "find_server",
    "publisher",
    "publishing_active",
    "publishing_applies",
    "wrap_publishing",
]
