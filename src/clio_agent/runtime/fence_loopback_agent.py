"""In-fence side of a Windows fenced server's loopback publishing (stdlib only, run by path).

CLIO starts a fenced stdio MCP server as ``python -I fence_loopback_agent.py -- <argv>``
inside Codex's native MXC container, which drops connections from the host to the
container's loopback listeners. This agent runs the server with its stdio piped through, and
keeps one connection out to CLIO's publisher (``CLIO_FENCE_LOOPBACK=<port>:<token>``):

* when the server writes a loopback address (``127.0.0.1:<port>``, ``localhost:<port>`` or
  ``[::1]:<port>``), the agent asks CLIO to publish that port. CLIO does so only when the
  server's own launch (its process tree, or its AppContainer) listens on it. The address becomes ``127.0.0.1:<host port>``,
  a fresh host port that reaches it; in a URL, when CLIO could also listen on ``[::1]`` with
  the same port, it becomes ``localhost:<port>`` instead, so a page that checks its own
  origin still accepts it. Host ports CLIO sends back are rewritten the other way, so the
  server keeps seeing its own ports;
* when CLIO asks it to dial one of those listeners, the agent connects to it from inside the
  container, opens a second connection out to CLIO and splices the two.

Nothing here listens. Without the publisher the server runs as before, unchanged.
"""

from __future__ import annotations

import contextlib
import os
import re
import socket
import subprocess
import sys
import threading
import time

VARIABLE = "CLIO_FENCE_LOOPBACK"
#: How the MXC fence's loopback works, stamped on its sandbox state (and so its records).
#: Inside it WMI refuses to start processes, and a job breakaway still leaves the child in the
#: fence's job, so a "detached" child lives while its server's launch does and ends with it.
MXC_LOOPBACK_DETAILS = {
    "host_loopback": "direct",
    "detached_children": "terminated",
    "inbound_loopback": "published",
}
_ADDRESS = re.compile(
    rb"(?P<url>://)?(?P<host>127\.0\.0\.1|localhost|\[::1\]):(?P<port>\d{1,5})(?!\d)"
)
_ANSWER_S = 3.0
_NEGATIVE_S = 5.0
_DIAL_S = 5.0


class Control:
    """The connection out to CLIO's publisher, and the ports it has published."""

    def __init__(self, port: int, token: str) -> None:
        self.port = port
        self.token = token
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=_DIAL_S)
        self.sock.settimeout(None)
        self.lock = threading.Lock()
        self.answered = threading.Condition()
        #: server port -> (host port on 127.0.0.1, whether [::1] has the same port)
        self.outward: dict[int, tuple[int, bool]] = {}
        self.inward: dict[int, int] = {}
        self.refused: dict[int, float] = {}
        self.send(f"HELLO {token} {os.getpid()}")
        threading.Thread(target=self._read, name="loopback-control", daemon=True).start()

    def send(self, line: str) -> None:
        with self.lock:
            self.sock.sendall(line.encode("ascii") + b"\n")

    def _read(self) -> None:
        with self.sock.makefile("rb") as lines:
            for raw in lines:
                parts = raw.decode("ascii", "replace").split()
                if len(parts) == 4 and parts[0] == "MAP":
                    with self.answered:
                        port, host_port = int(parts[1]), int(parts[2])
                        self.outward[port] = (host_port, parts[3] == "1")
                        self.inward[host_port] = port
                        self.answered.notify_all()
                elif len(parts) == 2 and parts[0] == "NONE":
                    with self.answered:
                        self.refused[int(parts[1])] = time.monotonic()
                        self.answered.notify_all()
                elif len(parts) == 4 and parts[0] == "DIAL":
                    threading.Thread(
                        target=self._dial, args=(parts[1], parts[2], int(parts[3])), daemon=True
                    ).start()

    def published(self, port: int) -> tuple[int, bool] | None:
        """How the host reaches ``port``, asking CLIO to publish it first if needed."""

        with self.answered:
            if port in self.outward:
                return self.outward[port]
            refused = self.refused.get(port)
            if refused is not None and time.monotonic() - refused < _NEGATIVE_S:
                return None
            self.refused.pop(port, None)
        self.send(f"PUB {port}")
        deadline = time.monotonic() + _ANSWER_S
        with self.answered:
            while port not in self.outward and port not in self.refused:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self.answered.wait(left)
            return self.outward.get(port)

    def outbound(self, url: bool, host: bytes, port: int) -> bytes | None:
        """The address the host uses for the server's ``host:port``, or ``None`` to keep it.

        A URL keeps its port when ``[::1]`` has it, as ``localhost:<port>``: that is still an
        origin the page accepts for itself. Anything else becomes ``127.0.0.1:<host port>``.
        """

        published = self.published(port)
        if published is None:
            return None
        host_port, same = published
        if url and same:
            return b"localhost:%d" % port
        return b"127.0.0.1:%d" % host_port

    def inbound(self, url: bool, host: bytes, port: int) -> bytes | None:
        """The server's own address for a host port CLIO sends back, or ``None`` to keep it."""

        del url
        original = self.inward.get(port)
        return None if original is None or host == b"[::1]" else b"%s:%d" % (host, original)

    def _dial(self, ident: str, host: str, port: int) -> None:
        try:
            inner = socket.create_connection((host, port), timeout=_DIAL_S)
        except OSError:
            self.send(f"FAIL {ident}")
            return
        try:
            outer = socket.create_connection(("127.0.0.1", self.port), timeout=_DIAL_S)
            outer.sendall(f"DATA {self.token} {ident}\n".encode("ascii"))
        except OSError:
            inner.close()
            self.send(f"FAIL {ident}")
            return
        inner.settimeout(None)
        outer.settimeout(None)
        splice(inner, outer)


def splice(first: socket.socket, second: socket.socket) -> None:
    """Copy both ways until each side has finished, then close both."""

    def copy(source: socket.socket, sink: socket.socket) -> None:
        with contextlib.suppress(OSError):  # either side closing ends this direction
            while chunk := source.recv(65536):
                sink.sendall(chunk)
        with contextlib.suppress(OSError):
            sink.shutdown(socket.SHUT_WR)

    other = threading.Thread(target=copy, args=(second, first), daemon=True)
    other.start()
    copy(first, second)
    other.join()
    first.close()
    second.close()


def rewrite(line: bytes, lookup) -> bytes:
    """``line`` with each loopback address that ``lookup(url, host, port)`` replaces."""

    def swap(match: re.Match[bytes]) -> bytes:
        url = match.group("url") is not None
        before = line[match.start() - 1 : match.start()] if match.start() else b""
        port = int(match.group("port"))
        if (not url and before and (before.isalnum() or before in b"._-")) or not (
            0 < port < 65536
        ):
            return match.group(0)
        mapped = lookup(url, match.group("host"), port)
        if mapped is None:
            return match.group(0)
        return (b"://" if url else b"") + mapped

    return _ADDRESS.sub(swap, line)


def _pump(source, sink, lookup) -> None:
    with contextlib.suppress(OSError, ValueError):  # the other end closed its stream
        while line := source.readline():
            sink.write(rewrite(line, lookup))
            sink.flush()
    with contextlib.suppress(OSError):
        sink.close()


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "--" or len(argv) < 2:
        print("usage: fence_loopback_agent.py -- command [args...]", file=sys.stderr)
        return 2
    command = argv[1:]
    setting = os.environ.pop(VARIABLE, "")
    port, _, token = setting.partition(":")
    control: Control | None = None
    if port.isdigit() and token:
        try:
            control = Control(int(port), token)
        except OSError as exc:
            print(f"CLIO loopback publishing unavailable: {exc}", file=sys.stderr, flush=True)
    if control is None:
        return subprocess.call(command)
    server = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    threading.Thread(
        target=_pump, args=(sys.stdin.buffer, server.stdin, control.inbound), daemon=True
    ).start()
    _pump(server.stdout, sys.stdout.buffer, control.outbound)
    return server.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
