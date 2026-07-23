"""The clio-owned network chokepoint — loopback CONNECT/HTTP passthrough + egress record (B4).

Owner decision #974.3/#974.7: every confined child's egress routes through ONE clio-owned
proxy, whose port is written into the srt config's ``network.httpProxyPort``. On the srt
tier the OS fence FORCES children through it (srt sets ``HTTP_PROXY``/``HTTPS_PROXY`` in the
child env AND the fence blocks direct sockets), so the proxy is enforcement, not just
cooperation; on the Landlock/floor tier the env proxy is COOPERATIVE (raw sockets bypass) —
each record says which, per-edge, never a false completeness claim.

**Transparent passthrough (B2)** — the proxy forwards BOTH proxied verbs a fenced client
emits: ``CONNECT host:port`` (the tunnel an HTTPS client opens — dial, ``200``, pump opaque
TLS, domain-level, no MITM/CA) AND absolute-form ``GET http://host/path`` (what a client
sends for a plain-HTTP target — a scientific MCP server's data source can be plain HTTP, e.g.
an NDP catalog on ``:8003``). A CONNECT-only proxy answered plain HTTP ``501`` and silently
broke every plain-HTTP fleet call under the fence — the B2 live gate caught it (#1019).

**Egress recording (B4)** — each forwarded connection (BOTH verbs) mints ONE ``net.egress``
semantic event ``{host, port, resolved_ip, child_id, mechanism, transport, at}`` via a
registered recorder (:func:`set_egress_recorder`, wired from the gact lifespan so this module
never imports the god app). Recording happens at connection OPEN — after the upstream dial,
OFF the per-byte pump — so a failed emit is a typed log that never wedges the pump nor breaks
egress. ``net.egress`` is durable-only, trace-only substrate (see
``gact.semantic_events.SSE_TRACE_ONLY_EVENT_TYPES``); the ``used web:domain@time`` ingest
edge is JOINED from it in the artifacts package.

**Per-child attribution (B4, owner decision #974 spike, srt source-verified).** srt, given an
external ``network.httpProxyPort`` (an int), sets the child's proxy env to
``http://localhost:<port>`` with **NO** credential — srt EXPLICITLY refuses to embed its
``proxyAuthToken`` for an external proxy ("embedding our token in its URL would be wrong",
``sandbox-manager.js``: ``proxyAuthToken = httpProxyPort !== undefined ? undefined : …``). A
per-child CREDENTIAL TOKEN therefore cannot survive srt's composition; a **per-child PORT**
does — each confined child gets its OWN loopback listener (:func:`Chokepoint.open_child_channel`),
that port is the child's ``httpProxyPort`` (srt tier: reached via srt's socat bridge) and the
child's ``HTTP(S)_PROXY`` env (floor/Landlock tier), so the listener a connection ARRIVES on
deterministically names the child. No timing heuristic.

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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

REASON_CHOKEPOINT_START_FAILED = "chokepoint_start_failed"

#: Net-mechanism labels stamped on each ``net.egress`` (honest per tier, owner #974.3/.7):
#: srt = OS fence forces the child through the proxy; Landlock/floor = env proxy only.
MECHANISM_PROXY_ENFORCED = "proxy-enforced"
MECHANISM_ENV_COOPERATIVE = "env-cooperative"

#: Loopback host the proxy binds (never a routable interface — clio-private).
_LOOPBACK = "127.0.0.1"
#: Bounded relay buffer; bidirectional pump copies in these chunks.
_RELAY_CHUNK = 64 * 1024
#: Socket timeout on the initial CONNECT line read (a stalled client never wedges a thread).
_CONNECT_READ_TIMEOUT_S = 30.0
#: Bound on live per-child channels (leak guard). A confined MCP fleet is small, so this is
#: generous. Over the cap a new child falls back to the SHARED listener (child_id="" —
#: unattributed, a typed log), never a silent leak and — critically — NEVER by evicting a
#: live child's listener (that would refuse its next connection and break its network; a dead
#: child's channel is reclaimed by :func:`Chokepoint.close_child_channel` on reap, a B5 seam).
_MAX_CHILD_CHANNELS = 128


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class EgressRecord:
    """One forwarded egress connection — the body of a ``net.egress`` semantic event.

    ``host`` is the requested authority host; ``resolved_ip`` is what the chokepoint's own
    ``getaddrinfo``/``create_connection`` resolved it to (the DNS side-channel record — the
    proxy resolves, the fenced child never issues raw UDP/53). ``child_id`` is the per-child
    channel the connection arrived on (``""`` = the shared/unattributed listener).
    ``mechanism`` is the tier's honest net enforcement (:data:`MECHANISM_PROXY_ENFORCED` vs
    :data:`MECHANISM_ENV_COOPERATIVE`). ``transport`` is ``connect`` (HTTPS tunnel) or
    ``http`` (absolute-form plain HTTP).
    """

    child_id: str
    host: str
    port: int
    resolved_ip: str
    transport: str
    mechanism: str
    workspace_root: str
    at: str


#: The registered egress recorder (set from the gact lifespan once ARC is live). ``None``
#: before wiring (or in a plain runtime/test process) → recording is a guarded no-op.
_RECORDER: Optional[Callable[[EgressRecord], None]] = None
_RECORDER_LOCK = threading.Lock()

#: The registered deny-mode egress GATE (B5 #979.5), consulted at CONNECT before the upstream
#: dial. ``None`` (default, or a plain runtime/test process) → allow-all passthrough (the B4
#: default is ALLOW + RECORD). Wired from the gact lifespan to a closure that consults the
#: workspace's opt-in deny mode + ``host_pattern`` policies and, on an unknown domain, opens
#: the interactive permission gate. Returns ``"allow"``/``"deny"``; anything else = allow (the
#: gate must never fail CLOSED on a wiring bug — deny mode is opt-in and gated separately).
_GATE: Optional[Callable[[EgressRecord], str]] = None
_GATE_LOCK = threading.Lock()


def set_egress_recorder(recorder: Optional[Callable[[EgressRecord], None]]) -> None:
    """Register (or clear with ``None``) the process egress recorder.

    Wired from the gact lifespan (after ARC is live) to a closure that appends to the
    per-app egress ledger AND emits ``net.egress`` via ``_emit_semantic_event`` — so this
    module never imports the god app. A recorder that raises is caught at the call site (a
    failed record must never wedge the pump nor break egress).
    """
    global _RECORDER
    with _RECORDER_LOCK:
        _RECORDER = recorder


def set_egress_gate(gate: Optional[Callable[[EgressRecord], str]]) -> None:
    """Register (or clear with ``None``) the deny-mode CONNECT gate (B5 #979.5).

    Wired from the gact lifespan to a closure over ``app`` (so this module never imports the
    god app). Consulted at connection OPEN, before the upstream dial: a workspace NOT in deny
    mode returns ``"allow"`` (the B4 default), so wiring the gate is inert until a workspace
    opts in. A gate that raises is treated as allow (fail-open on a wiring bug — the deny
    decision itself is the only thing that blocks).
    """
    global _GATE
    with _GATE_LOCK:
        _GATE = gate


class ChokepointStartError(RuntimeError):
    """The chokepoint could not bind/listen — typed (``chokepoint_start_failed``)."""

    def __init__(self, message: str, *, reason: str = REASON_CHOKEPOINT_START_FAILED) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass
class _Channel:
    """A per-child loopback listener + its attribution metadata."""

    child_id: str
    listener: socket.socket
    port: int
    thread: threading.Thread
    mechanism: str
    workspace_root: str


class Chokepoint:
    """A running loopback CONNECT/HTTP passthrough that RECORDS each forwarded egress (B4).

    A single shared listener (bind viability + unattributed fallback) plus N per-child
    listeners (:meth:`open_child_channel`) that name the producing child. Bind happens in
    :meth:`start` (raising :class:`ChokepointStartError` on failure) so a caller can degrade
    the ladder rung on a typed reason. Accept loops + per-connection pumps run on daemon
    threads; :meth:`stop` closes every listener and joins them.
    """

    def __init__(self) -> None:
        self._listener: Optional[socket.socket] = None
        self._port = 0
        self._accept_thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._conns: list[socket.socket] = []
        self._conns_lock = threading.Lock()
        self._channels: dict[str, _Channel] = {}
        self._channels_lock = threading.Lock()

    @property
    def port(self) -> int:
        """The bound shared loopback TCP port (0 until started)."""
        return self._port

    def start(self) -> "Chokepoint":
        """Bind + listen on ``127.0.0.1:0`` and start the accept loop. Typed failure."""
        self._listener = self._bind_listener()
        self._port = int(self._listener.getsockname()[1])
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            args=(self._listener, ""),
            name="clio-net-chokepoint",
            daemon=True,
        )
        self._accept_thread.start()
        logger.info(
            "net chokepoint listening host=%s port=%d (allow-all CONNECT/HTTP, recording)",
            _LOOPBACK,
            self._port,
        )
        return self

    @staticmethod
    def _bind_listener() -> socket.socket:
        """Bind + listen a fresh loopback listener on an ephemeral port. Typed failure."""
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((_LOOPBACK, 0))
            listener.listen(128)
        except OSError as exc:
            raise ChokepointStartError(f"chokepoint bind/listen failed: {exc}") from exc
        return listener

    # --- per-child channels (deterministic attribution, B4) ------------------ #
    def open_child_channel(
        self, child_id: str, *, mechanism: str = "", workspace_root: str = ""
    ) -> int:
        """Open (idempotent) a per-child listener and return its loopback port.

        The port is the child's ``httpProxyPort`` (srt tier) / ``HTTP(S)_PROXY`` (floor tier);
        every connection accepted on it is recorded with THIS ``child_id`` (deterministic, no
        timing heuristic). Idempotent per ``child_id`` (a re-open refreshes metadata, reuses
        the port). Bounded by :data:`_MAX_CHILD_CHANNELS`: over the cap the caller gets ``0``
        and falls back to the shared listener (unattributed) — a typed log, never a silent
        leak and never by evicting a live child's listener (which would break its network; a
        dead child's channel is reclaimed by :meth:`close_child_channel` on reap, a B5 seam).
        """
        with self._channels_lock:
            existing = self._channels.get(child_id)
            if existing is not None:
                existing.mechanism = mechanism or existing.mechanism
                existing.workspace_root = workspace_root or existing.workspace_root
                return existing.port
            if len(self._channels) >= _MAX_CHILD_CHANNELS:
                logger.warning(
                    "net chokepoint child channel refused reason=channel_cap_reached child=%s "
                    "(egress falls back to the shared unattributed listener)",
                    child_id,
                )
                return 0
            try:
                listener = self._bind_listener()
            except ChokepointStartError as exc:
                logger.warning(
                    "net chokepoint child channel bind failed reason=%s child=%s error=%r",
                    exc.reason,
                    child_id,
                    exc,
                )
                return 0
            port = int(listener.getsockname()[1])
            thread = threading.Thread(
                target=self._accept_loop,
                args=(listener, child_id),
                name=f"clio-net-chokepoint-{child_id[:16]}",
                daemon=True,
            )
            self._channels[child_id] = _Channel(
                child_id=child_id,
                listener=listener,
                port=port,
                thread=thread,
                mechanism=mechanism,
                workspace_root=workspace_root,
            )
            thread.start()
            return port

    def close_child_channel(self, child_id: str) -> None:
        """Close + drop a per-child channel (idempotent; called when the child is reaped)."""
        with self._channels_lock:
            channel = self._channels.pop(child_id, None)
        if channel is not None:
            _safe_close(channel.listener)
            channel.thread.join(timeout=1.0)

    def _accept_loop(self, listener: socket.socket, child_id: str) -> None:
        while not self._stopping.is_set():
            try:
                client, _addr = listener.accept()
            except OSError:
                break  # listener closed by stop()/close_child_channel()
            threading.Thread(
                target=self._handle,
                args=(client, child_id),
                name="clio-net-chokepoint-conn",
                daemon=True,
            ).start()

    def _handle(self, client: socket.socket, child_id: str = "") -> None:
        """Serve one client: CONNECT tunnel (HTTPS) OR absolute-form HTTP forward. Guarded.

        The passthrough must be TRANSPARENT to the fleet — a scientific MCP server may talk
        to a plain-HTTP data source (e.g. an NDP catalog on ``:8003``), which a proxied
        client sends as an absolute-form ``GET http://host/path`` line, NOT a ``CONNECT``.
        Both verbs forward here and each is RECORDED as ``net.egress`` at connection OPEN
        (off the pump), attributed to ``child_id``.
        """
        upstream: Optional[socket.socket] = None
        try:
            client.settimeout(_CONNECT_READ_TIMEOUT_S)
            header = self._read_request_head(client)
            target = _parse_connect_target(header)
            if target is not None:
                # HTTPS: open the tunnel, then pump opaque TLS bytes (domain-level only).
                host, port = target
                # Deny-mode gate (B5 #979.5): consult BEFORE dialing so a blocked domain never
                # opens an upstream socket. Allow-all when no gate is wired (B4 default).
                if not self._gate_allows(child_id, host, port, "connect"):
                    client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                    return
                upstream = socket.create_connection((host, port), timeout=_CONNECT_READ_TIMEOUT_S)
                self._record_open(child_id, host, port, upstream, "connect")
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
            if not self._gate_allows(child_id, host, port, "http"):
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            upstream = socket.create_connection((host, port), timeout=_CONNECT_READ_TIMEOUT_S)
            self._record_open(child_id, host, port, upstream, "http")
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

    def _record_open(
        self, child_id: str, host: str, port: int, upstream: socket.socket, transport: str
    ) -> None:
        """Mint ONE ``net.egress`` record at connection OPEN — off the pump, fully guarded.

        The recording is deliberately at establishment (not per-byte): it captures every
        forwarded connection, including long-lived ones, without touching the hot byte-copy
        path. A missing recorder (plain runtime/test) or a raising recorder is a typed log
        that NEVER breaks egress (the fence must not depend on the trace being wired).
        """
        recorder = _RECORDER
        if recorder is None:
            return
        try:
            resolved_ip = ""
            try:
                resolved_ip = str(upstream.getpeername()[0])
            except OSError:
                pass
            mechanism, workspace_root = self._channel_attribution(child_id)
            recorder(
                EgressRecord(
                    child_id=child_id,
                    host=host,
                    port=int(port),
                    resolved_ip=resolved_ip,
                    transport=transport,
                    mechanism=mechanism,
                    workspace_root=workspace_root,
                    at=_utcnow_iso(),
                )
            )
        except Exception as exc:  # noqa: BLE001 — a record must never wedge the pump/egress
            logger.debug(
                "net egress record skipped reason=egress_record_failed child=%s host=%s error=%r",
                child_id,
                host,
                exc,
            )

    def _gate_allows(self, child_id: str, host: str, port: int, transport: str) -> bool:
        """Consult the deny-mode gate for one CONNECT (B5 #979.5). ``True`` = allow.

        Builds the pre-dial :class:`EgressRecord` the gate decides on (``resolved_ip=""`` — not
        dialed yet), attributed to the child's channel. No gate wired → allow (the genuinely
        UNWIRED case = the B4 ALLOW + RECORD default). The wired gate itself
        (``grants._egress_gate_decision``) is FAIL-CLOSED for every in-deny-mode path — a deny
        workspace whose decision errors returns ``"deny"`` with a typed reason, never reaching
        this catch. This residual ``except`` is the last resort for a gate that crashes ENTIRELY
        (an unrecognised/foreign gate, or record construction) — equivalent to unwired — so a
        total wiring failure degrades to the B4 default rather than severing all egress.
        """
        gate = _GATE
        if gate is None:
            return True
        mechanism, workspace_root = self._channel_attribution(child_id)
        try:
            decision = gate(
                EgressRecord(
                    child_id=child_id,
                    host=host,
                    port=int(port),
                    resolved_ip="",
                    transport=transport,
                    mechanism=mechanism,
                    workspace_root=workspace_root,
                    at=_utcnow_iso(),
                )
            )
        except Exception as exc:  # noqa: BLE001 — a gate wiring bug must never sever egress
            logger.debug(
                "net egress gate errored (allowing) reason=egress_gate_failed host=%s error=%r",
                host,
                exc,
            )
            return True
        return decision != "deny"

    def _channel_attribution(self, child_id: str) -> tuple[str, str]:
        """The ``(mechanism, workspace_root)`` recorded for ``child_id`` (``("","")`` if none)."""
        if not child_id:
            return "", ""
        with self._channels_lock:
            channel = self._channels.get(child_id)
            if channel is None:
                return "", ""
            return channel.mechanism, channel.workspace_root

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
        """Close every listener + all live tunnels and join the accept loops (idempotent)."""
        self._stopping.set()
        if self._listener is not None:
            _safe_close(self._listener)
            self._listener = None
        with self._channels_lock:
            channels = list(self._channels.values())
            self._channels.clear()
        for channel in channels:
            _safe_close(channel.listener)
        with self._conns_lock:
            conns = list(self._conns)
            self._conns.clear()
        for sock in conns:
            _safe_close(sock)
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
            self._accept_thread = None
        for channel in channels:
            channel.thread.join(timeout=1.0)


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
    """The running chokepoint's shared port, or ``None`` when none is installed."""
    return _CHOKEPOINT.port if _CHOKEPOINT is not None else None


def open_child_channel(child_id: str, *, mechanism: str = "", workspace_root: str = "") -> int:
    """Open a per-child egress channel on the process chokepoint (installing it if needed).

    Returns the per-child loopback port, or ``0`` when no channel could be opened (the caller
    falls back to the shared/unattributed listener with a typed reason). Installing lazily
    here means the Landlock (env-cooperative) tier — which does not start the proxy during
    ladder resolution — still gets a chokepoint the first time a child is wrapped.
    """
    try:
        chokepoint = install_chokepoint()
    except ChokepointStartError as exc:
        logger.warning(
            "net chokepoint child channel skipped reason=%s child=%s error=%r",
            exc.reason,
            child_id,
            exc,
        )
        return 0
    return chokepoint.open_child_channel(
        child_id, mechanism=mechanism, workspace_root=workspace_root
    )


def close_child_channel(child_id: str) -> None:
    """Close a per-child egress channel on the process chokepoint (idempotent, guarded)."""
    chokepoint = _CHOKEPOINT
    if chokepoint is not None:
        chokepoint.close_child_channel(child_id)


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
    "MECHANISM_ENV_COOPERATIVE",
    "MECHANISM_PROXY_ENFORCED",
    "REASON_CHOKEPOINT_START_FAILED",
    "Chokepoint",
    "ChokepointStartError",
    "EgressRecord",
    "chokepoint_port",
    "close_child_channel",
    "current_chokepoint",
    "install_chokepoint",
    "open_child_channel",
    "set_egress_gate",
    "set_egress_recorder",
    "shutdown_chokepoint",
]
