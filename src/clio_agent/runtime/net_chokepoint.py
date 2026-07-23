"""The clio-owned network chokepoint — a minimal loopback CONNECT passthrough (#976/B2).

Owner decision #974.3/#974.7: every confined child's egress routes through ONE clio-owned
proxy, whose port is written into the srt config's ``network.httpProxyPort``. On the srt
tier the OS fence FORCES children through it (srt sets ``HTTP_PROXY``/``HTTPS_PROXY`` in the
child env AND the fence blocks direct sockets), so the proxy is enforcement, not just
cooperation.

**B2 SCOPE — allow-all TRANSPARENT passthrough, NO recording, NO policy.** This slice ships
the minimal proxy srt's ``httpProxyPort`` requires (srt's own note: "The external proxy must
handle domain filtering"). It forwards BOTH proxied verbs a fenced client emits: ``CONNECT
host:port`` (the tunnel an HTTPS client opens — dial, ``200``, pump opaque TLS, domain-level,
no MITM/CA) AND absolute-form ``GET http://host/path`` (what a client sends for a plain-HTTP
target — a scientific MCP server's data source can be plain HTTP, e.g. an NDP catalog on
``:8003``). A CONNECT-only proxy answered plain HTTP ``501`` and silently broke every
plain-HTTP fleet call under the fence — the B2 live gate caught it. **B4 owns this module's
growth**: it turns each forwarded egress into a recorded ``net.egress`` → ``used
web:domain@time`` provenance edge and adds the opt-in deny-by-default policy. B2 forwards
transparently; it does not record or gate.

NO SILENT FALLBACK: a bind/listen failure raises a typed :class:`ChokepointStartError`
(``chokepoint_start_failed``); the ladder degrades the RUNG (srt → landlock/floor) rather
than starting srt children that would silently lose all network. Threads are daemon +
explicitly joined on :func:`shutdown_chokepoint` (wired to the server lifespan) with an
``atexit`` backstop, so the proxy never outlives the server.
"""

from __future__ import annotations

import atexit
import logging
import socket
import threading
from typing import Optional

logger = logging.getLogger(__name__)

REASON_CHOKEPOINT_START_FAILED = "chokepoint_start_failed"

#: Loopback host the proxy binds (never a routable interface — clio-private).
_LOOPBACK = "127.0.0.1"
#: Bounded relay buffer; bidirectional pump copies in these chunks.
_RELAY_CHUNK = 64 * 1024
#: Socket timeout on the initial CONNECT line read (a stalled client never wedges a thread).
_CONNECT_READ_TIMEOUT_S = 30.0


class ChokepointStartError(RuntimeError):
    """The chokepoint could not bind/listen — typed (``chokepoint_start_failed``)."""

    def __init__(self, message: str, *, reason: str = REASON_CHOKEPOINT_START_FAILED) -> None:
        super().__init__(message)
        self.reason = reason


class Chokepoint:
    """A running loopback CONNECT passthrough proxy (allow-all, no recording — B2).

    Bind happens in :meth:`start` (raising :class:`ChokepointStartError` on failure) so a
    caller can degrade the ladder rung on a typed reason. The accept loop + per-connection
    pumps run on daemon threads; :meth:`stop` closes the listener and joins them.
    """

    def __init__(self) -> None:
        self._listener: Optional[socket.socket] = None
        self._port = 0
        self._accept_thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._conns: list[socket.socket] = []
        self._conns_lock = threading.Lock()

    @property
    def port(self) -> int:
        """The bound loopback TCP port (0 until started)."""
        return self._port

    def start(self) -> "Chokepoint":
        """Bind + listen on ``127.0.0.1:0`` and start the accept loop. Typed failure."""
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((_LOOPBACK, 0))
            listener.listen(128)
        except OSError as exc:
            raise ChokepointStartError(f"chokepoint bind/listen failed: {exc}") from exc
        self._listener = listener
        self._port = int(listener.getsockname()[1])
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="clio-net-chokepoint", daemon=True
        )
        self._accept_thread.start()
        logger.info(
            "net chokepoint listening host=%s port=%d (allow-all CONNECT, B2)",
            _LOOPBACK,
            self._port,
        )
        return self

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stopping.is_set():
            try:
                client, _addr = self._listener.accept()
            except OSError:
                break  # listener closed by stop()
            threading.Thread(
                target=self._handle, args=(client,), name="clio-net-chokepoint-conn", daemon=True
            ).start()

    def _handle(self, client: socket.socket) -> None:
        """Serve one client: CONNECT tunnel (HTTPS) OR absolute-form HTTP forward. Guarded.

        The passthrough must be TRANSPARENT to the fleet — a scientific MCP server may talk
        to a plain-HTTP data source (e.g. an NDP catalog on ``:8003``), which a proxied
        client sends as an absolute-form ``GET http://host/path`` line, NOT a ``CONNECT``.
        A CONNECT-only proxy answered those ``501 Not Implemented``, silently breaking every
        plain-HTTP fleet call under the fence (caught by the B2 live gate). Both verbs
        forward here; RECORDING each as a ``net.egress`` edge is still B4's addition.
        """
        upstream: Optional[socket.socket] = None
        try:
            client.settimeout(_CONNECT_READ_TIMEOUT_S)
            header = self._read_request_head(client)
            target = _parse_connect_target(header)
            if target is not None:
                # HTTPS: open the tunnel, then pump opaque TLS bytes (domain-level only).
                host, port = target
                upstream = socket.create_connection((host, port), timeout=_CONNECT_READ_TIMEOUT_S)
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                client.settimeout(None)
                self._track(client)
                self._track(upstream)
                self._pump(client, upstream)
                return
            forward = _parse_absolute_form(header)
            if forward is None:
                client.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            # Plain HTTP: dial the origin, replay the head rewritten to origin-form, pump.
            host, port, origin_head = forward
            upstream = socket.create_connection((host, port), timeout=_CONNECT_READ_TIMEOUT_S)
            upstream.sendall(origin_head)
            client.settimeout(None)
            self._track(client)
            self._track(upstream)
            self._pump(client, upstream)
        except (OSError, ValueError):
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except OSError:
                pass
        finally:
            self._untrack(client)
            _safe_close(client)
            if upstream is not None:
                self._untrack(upstream)
                _safe_close(upstream)

    @staticmethod
    def _read_request_head(sock: socket.socket) -> bytes:
        """Read up to the CRLFCRLF end of the request head (bounded)."""
        data = b""
        while b"\r\n\r\n" not in data and len(data) < 16 * 1024:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        return data

    def _pump(self, a: socket.socket, b: socket.socket) -> None:
        """Bidirectionally copy bytes between two sockets until either closes."""
        t = threading.Thread(target=_copy, args=(a, b), daemon=True)
        t.start()
        _copy(b, a)
        t.join(timeout=1.0)

    def _track(self, sock: socket.socket) -> None:
        with self._conns_lock:
            self._conns.append(sock)

    def _untrack(self, sock: socket.socket) -> None:
        with self._conns_lock:
            if sock in self._conns:
                self._conns.remove(sock)

    def stop(self) -> None:
        """Close the listener + all live tunnels and join the accept loop (idempotent)."""
        self._stopping.set()
        if self._listener is not None:
            _safe_close(self._listener)
            self._listener = None
        with self._conns_lock:
            conns = list(self._conns)
            self._conns.clear()
        for sock in conns:
            _safe_close(sock)
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None


def _copy(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            chunk = src.recv(_RELAY_CHUNK)
            if not chunk:
                break
            dst.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _safe_close(sock: socket.socket) -> None:
    try:
        sock.close()
    except OSError:
        pass


def _parse_connect_target(header: bytes) -> Optional[tuple[str, int]]:
    """Parse ``CONNECT host:port HTTP/1.1`` → ``(host, port)``; ``None`` when not a CONNECT.

    CONNECT is the tunnel every HTTPS client opens through a proxy — the domain-level
    visibility tier (no MITM). A malformed target or a non-CONNECT verb returns ``None``.
    """
    try:
        line = header.split(b"\r\n", 1)[0].decode("latin-1")
    except (IndexError, UnicodeDecodeError):
        return None
    parts = line.split()
    if len(parts) < 2 or parts[0].upper() != "CONNECT":
        return None
    authority = parts[1]
    host, _, port_s = authority.rpartition(":")
    if not host or not port_s.isdigit():
        return None
    port = int(port_s)
    if not (0 < port < 65536):
        return None
    return host, port


def _parse_absolute_form(header: bytes) -> Optional[tuple[str, int, bytes]]:
    """Parse a proxied plain-HTTP request → ``(host, port, origin_head)``; ``None`` if not.

    A client configured with ``HTTP_PROXY`` sends the request line in absolute form —
    ``GET http://host:port/path HTTP/1.1`` — to the proxy. This rewrites the head to the
    origin form the target server expects (``GET /path HTTP/1.1``), preserving the method,
    the rest of the path/query, the HTTP version and every header (incl. ``Host``), so the
    proxy is a transparent passthrough. Non-HTTP-URL targets (or a malformed head) → ``None``.
    Only the request HEAD is rewritten; any body streams verbatim through :meth:`_pump`.
    """
    try:
        head_text = header.decode("latin-1")
    except UnicodeDecodeError:
        return None
    line, sep, rest = head_text.partition("\r\n")
    if not sep:
        return None
    parts = line.split(" ")
    if len(parts) != 3:
        return None
    method, target, version = parts
    if not target.lower().startswith("http://"):
        return None
    without_scheme = target[len("http://") :]
    authority, slash, path = without_scheme.partition("/")
    origin_path = f"/{path}" if slash else "/"
    host, _, port_s = authority.partition(":")
    if not host:
        return None
    if port_s:
        if not port_s.isdigit() or not (0 < int(port_s) < 65536):
            return None
        port = int(port_s)
    else:
        port = 80
    origin_head = f"{method} {origin_path} {version}\r\n{rest}".encode("latin-1")
    return host, port, origin_head


# Process-lifetime singleton (same pattern as the child reaper / sandbox state).           #
_CHOKEPOINT: Optional[Chokepoint] = None
_LOCK = threading.Lock()


def install_chokepoint() -> Chokepoint:
    """Start (once) the process chokepoint and return it. Raises :class:`ChokepointStartError`.

    Idempotent: a second call returns the running instance. Registers an ``atexit`` backstop
    so a server that forgot to call :func:`shutdown_chokepoint` still tears the proxy down.
    """
    global _CHOKEPOINT
    with _LOCK:
        if _CHOKEPOINT is not None:
            return _CHOKEPOINT
        chokepoint = Chokepoint().start()
        _CHOKEPOINT = chokepoint
        atexit.register(shutdown_chokepoint)
        return chokepoint


def current_chokepoint() -> Optional[Chokepoint]:
    """The running chokepoint, or ``None`` when none is installed in this process."""
    return _CHOKEPOINT


def chokepoint_port() -> Optional[int]:
    """The running chokepoint's port, or ``None`` when none is installed."""
    return _CHOKEPOINT.port if _CHOKEPOINT is not None else None


def shutdown_chokepoint() -> None:
    """Stop + clear the process chokepoint (idempotent; wired to the server lifespan)."""
    global _CHOKEPOINT
    with _LOCK:
        if _CHOKEPOINT is None:
            return
        try:
            _CHOKEPOINT.stop()
        except Exception:  # noqa: BLE001 — teardown must never raise into shutdown
            logger.debug("net chokepoint stop raised during shutdown", exc_info=True)
        _CHOKEPOINT = None


__all__ = [
    "REASON_CHOKEPOINT_START_FAILED",
    "Chokepoint",
    "ChokepointStartError",
    "chokepoint_port",
    "current_chokepoint",
    "install_chokepoint",
    "shutdown_chokepoint",
]
