"""Reach SSH through the fence: a CONNECT helper and the ``ssh``/``scp``/``sftp`` launcher (RL37).

A fenced shell has no direct route out: its traffic leaves only through the fence's proxy,
which the child finds in ``HTTPS_PROXY``/``ALL_PROXY``. OpenSSH does not read those variables,
so this file gives it a ``ProxyCommand`` that does:

* ``net_connect.py <shim-dir> connect <host> <port>`` opens ``CONNECT host:port`` through the
  proxy (HTTP, or SOCKS5 when the variable says so) and joins standard input and output to the
  tunnel. With no proxy in the environment it dials the host directly.
* ``net_connect.py <shim-dir> ssh|scp|sftp <args>`` runs the real program with that helper as
  its ``ProxyCommand``. ``scp`` and ``sftp`` get ``-S <shim-dir>/ssh``. A ``ProxyJump`` is
  rewritten as a chain of these launchers, so every hop crosses the proxy too.

The proxy records each tunnel as ``net.egress`` with the host and port, and a host the policy
refuses gets ``403``, which the helper reports on standard error.

Behind the Linux fence the child's proxy is Codex's, which reaches only public hosts and sends
nothing else to CLIO. There CLIO also sets :data:`RELAY_VARIABLE`: the helper then opens a
tunnel to :data:`RELAY_AUTHORITY`, an IPv6 documentation address that is never routed, which
Codex passes to CLIO's proxy like any public host. CLIO's proxy answers that tunnel itself and
reads the real ``CONNECT`` inside it, so a private or local host can still be reached, after
the person allows it.

This module runs as a script on CLIO's interpreter in isolated mode (``python -I``), so it
imports only the standard library: it must start fast inside the fence and never import CLIO.
:mod:`clio_agent.runtime.fence_ssh` writes the ``ssh``/``scp``/``sftp`` shims that call it.
"""

from __future__ import annotations

import base64
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
from urllib.parse import unquote, urlsplit

#: Tools this launcher wraps; the shim directory holds one shim for each.
TOOLS = ("ssh", "scp", "sftp")

#: Proxy variables in the order the helper reads them; the first non-empty one wins.
PROXY_VARIABLES = (
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "HTTP_PROXY",
    "http_proxy",
)

#: Names the relay tunnel when the fence's proxy reaches only public hosts (the Linux fence).
RELAY_VARIABLE = "CLIO_EGRESS_RELAY"
#: The relay tunnel's target: in 2001:db8::/32 (RFC 3849), so it never reaches a real host.
#: CLIO's proxy answers it and reads the real ``CONNECT`` from inside the tunnel.
RELAY_HOST = "2001:db8::c110"
RELAY_PORT = 443
RELAY_AUTHORITY = f"[{RELAY_HOST}]:{RELAY_PORT}"

#: ssh(1) short options that take a value (OpenSSH 8 to 10 getopt string).
_SSH_VALUE_FLAGS = frozenset("BbcDEeFIiJLlmOoPpQRSWw")
#: scp(1) and sftp(1) short options that take a value.
_COPY_VALUE_FLAGS = {"scp": frozenset("cDFiJloPSX"), "sftp": frozenset("BbcDFiJloPRSsX")}
#: Bound on a proxy's reply head; a larger head is not a proxy answering CONNECT.
_MAX_REPLY_HEAD = 64 * 1024
_CHUNK = 64 * 1024
_DIAL_TIMEOUT_S = 30.0
#: After this long without a reply to ``CONNECT`` the helper says it is waiting: CLIO holds the
#: tunnel while the person answers a network permission request, however long that takes.
_APPROVAL_NOTICE_S = 5.0


class ProxyRefused(OSError):
    """The proxy answered ``CONNECT`` with anything but ``200``.

    ``fence_error`` is Codex's ``x-proxy-error`` when the Linux fence's own proxy refused.
    """

    def __init__(self, status: int, line: str) -> None:
        super().__init__(line)
        self.status = status
        self.line = line
        self.fence_error = ""


def _say(message: str) -> None:
    sys.stderr.write(f"clio: {message}\n")
    sys.stderr.flush()


# --------------------------------------------------------------------------- #
# The CONNECT helper.
# --------------------------------------------------------------------------- #


def proxy_from_environment(env: dict[str, str] | os._Environ[str]) -> str:
    """The proxy URL the fence gave this process, or ``""`` when it gave none."""
    for key in PROXY_VARIABLES:
        value = (env.get(key) or "").strip()
        if value:
            return value if "://" in value else f"http://{value}"
    return ""


def _authority(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"


def _read_head(sock: socket.socket, waiting_for: str = "") -> tuple[bytes, bytes]:
    """Read a reply head up to the blank line; return ``(head, bytes after it)``.

    With ``waiting_for`` set, a slow reply is reported once and then awaited without a limit.
    """
    data = b""
    while b"\r\n\r\n" not in data:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            if not waiting_for:
                raise
            _say(f"waiting for CLIO's proxy to open {waiting_for} (it may be asking to allow it)")
            waiting_for = ""
            sock.settimeout(None)
            continue
        if not chunk:
            break
        data += chunk
        if len(data) > _MAX_REPLY_HEAD:
            raise ProxyRefused(0, "the proxy reply head is too large")
    head, _sep, rest = data.partition(b"\r\n\r\n")
    return head, rest


def _http_connect(proxy: str, host: str, port: int, relay: str = "") -> tuple[socket.socket, bytes]:
    """``CONNECT host:port`` through ``proxy``; with ``relay``, inside a tunnel to it."""
    parts = urlsplit(proxy)
    sock = socket.create_connection(
        (parts.hostname or "127.0.0.1", parts.port or 8080), timeout=_DIAL_TIMEOUT_S
    )
    auth: list[str] = []
    if parts.username is not None:
        token = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
        auth.append("Proxy-Authorization: Basic " + base64.b64encode(token.encode()).decode())
    try:
        if relay:
            _connect_request(sock, relay, auth, wait=False)
            auth = []
        return sock, _connect_request(sock, _authority(host, port), auth, wait=True)
    except BaseException:
        sock.close()
        raise


def _connect_request(sock: socket.socket, target: str, auth: list[str], *, wait: bool) -> bytes:
    """Send ``CONNECT target`` on ``sock`` and return the bytes after a ``200`` reply head."""
    lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}", *auth]
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    sock.settimeout(_APPROVAL_NOTICE_S if wait else _DIAL_TIMEOUT_S)
    head, rest = _read_head(sock, waiting_for=target if wait else "")
    status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace").strip()
    fields = status_line.split()
    status = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else 0
    if status != 200:
        refused = ProxyRefused(status, status_line or "the proxy closed the connection")
        refused.fence_error = _header(head, "x-proxy-error")
        raise refused
    if not wait and rest:
        raise ProxyRefused(0, "the relay tunnel sent data before the real CONNECT")
    return rest


def _header(head: bytes, name: str) -> str:
    """The value of header ``name`` in a reply head (``""`` when absent)."""
    for raw in head.split(b"\r\n")[1:]:
        key, _sep, value = raw.decode("latin-1", "replace").partition(":")
        if key.strip().lower() == name:
            return value.strip()
    return ""


def _socks5_connect(proxy: str, host: str, port: int) -> tuple[socket.socket, bytes]:
    parts = urlsplit(proxy)
    sock = socket.create_connection(
        (parts.hostname or "127.0.0.1", parts.port or 1080), timeout=_DIAL_TIMEOUT_S
    )
    user = unquote(parts.username or "")
    methods = b"\x00\x02" if user else b"\x00"
    sock.sendall(b"\x05" + bytes([len(methods)]) + methods)
    reply = _recv_exact(sock, 2)
    if reply[1] == 0x02:
        password = unquote(parts.password or "")
        sock.sendall(
            b"\x01"
            + bytes([len(user)])
            + user.encode()
            + bytes([len(password)])
            + password.encode()
        )
        if _recv_exact(sock, 2)[1] != 0:
            sock.close()
            raise ProxyRefused(407, "the SOCKS proxy refused the credentials")
    elif reply[1] != 0x00:
        sock.close()
        raise ProxyRefused(0, "the SOCKS proxy offered no usable sign-in method")
    name = host.strip("[]").encode("idna")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
    head = _recv_exact(sock, 4)
    if head[1] != 0:
        sock.close()
        # SOCKS5 0x02 is "connection not allowed by ruleset": the policy refused the host.
        raise ProxyRefused(403 if head[1] == 0x02 else 502, f"SOCKS5 reply {head[1]}")
    length = {1: 4, 4: 16}.get(head[3]) or _recv_exact(sock, 1)[0]
    _recv_exact(sock, length + 2)
    return sock, b""


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ProxyRefused(0, "the proxy closed the connection")
        data += chunk
    return data


def open_tunnel(host: str, port: int, proxy: str, relay: str = "") -> tuple[socket.socket, bytes]:
    """A socket joined to ``host:port``: through ``proxy`` when one is set, else direct.

    ``relay`` (an HTTP proxy only) names the tunnel CLIO's proxy answers itself; the real
    ``CONNECT`` then travels inside it.
    """
    if not proxy:
        return socket.create_connection((host, port), timeout=_DIAL_TIMEOUT_S), b""
    scheme = urlsplit(proxy).scheme.lower()
    if scheme.startswith("socks"):
        return _socks5_connect(proxy, host, port)
    return _http_connect(proxy, host, port, relay)


def _pump(sock: socket.socket, first: bytes) -> bool:
    """Join standard input and output to ``sock`` until the far side closes.

    Returns whether the far side sent anything at all.
    """
    sock.settimeout(None)
    stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()

    def upstream() -> None:
        try:
            while chunk := os.read(stdin, _CHUNK):
                sock.sendall(chunk)
        except OSError:
            pass
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    threading.Thread(target=upstream, daemon=True).start()
    answered = bool(first)
    if first:
        os.write(stdout, first)
    while chunk := sock.recv(_CHUNK):
        answered = True
        view = memoryview(chunk)
        while view:
            view = view[os.write(stdout, view) :]
    return answered


def connect_main(host: str, port_text: str) -> int:
    """``connect <host> <port>``: the ``ProxyCommand`` body."""
    try:
        port = int(port_text)
    except ValueError:
        _say(f"port {port_text!r} is not a number")
        return 2
    proxy = proxy_from_environment(os.environ)
    relay = (os.environ.get(RELAY_VARIABLE) or "").strip()
    try:
        sock, first = open_tunnel(host, port, proxy, relay)
    except ProxyRefused as exc:
        if exc.fence_error:
            _say(
                f"the Linux sandbox refused {_authority(host, port)} ({exc.fence_error}): it "
                "reaches only public hosts, through CLIO's proxy; loopback, private and "
                "unresolvable addresses are blocked"
            )
        elif exc.status == 403 and relay:
            _say(
                f"CLIO refused {_authority(host, port)}: its network policy does not allow it, "
                "the request to allow it was declined, or it is CLIO's own service"
            )
        elif exc.status == 403:
            _say(f"the sandbox network policy refused {_authority(host, port)} ({exc.line})")
        else:
            _say(f"the sandbox proxy did not open {_authority(host, port)} ({exc.line})")
        return 1
    except OSError as exc:
        route = "through the sandbox proxy" if proxy else "directly"
        _say(f"could not reach {_authority(host, port)} {route}: {exc}")
        return 1
    try:
        answered = _pump(sock, first)
    except OSError:
        return 1
    finally:
        sock.close()
    if proxy and not answered:
        if relay:
            _say(f"{_authority(host, port)} closed the connection before it answered")
            return 1
        # Behind the Linux fence Codex's proxy accepts the tunnel before CLIO decides, so a
        # refusal or an unreachable host shows up here as a tunnel closed before any byte.
        _say(
            f"the tunnel to {_authority(host, port)} closed before the server answered: CLIO's "
            "network policy refused it, the request was declined, or the host did not answer"
        )
        return 1
    return 0


# --------------------------------------------------------------------------- #
# The ssh / scp / sftp launcher.
# --------------------------------------------------------------------------- #


def real_program(name: str, shim_dir: str) -> str:
    """The real ``name`` on ``PATH``, skipping the shim directory."""
    shim = os.path.normcase(os.path.realpath(shim_dir))
    path = os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if entry and os.path.normcase(os.path.realpath(entry)) != shim
    )
    return shutil.which(name, path=path) or ""


def split_ssh_args(
    args: list[str], value_flags: frozenset[str] = _SSH_VALUE_FLAGS
) -> tuple[list[str], list[str]]:
    """Split arguments into ``(options, operands)`` like getopt with ``value_flags``."""
    options: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "--":
            return options, args[index + 1 :]
        if not arg.startswith("-") or arg == "-":
            break
        options.append(arg)
        index += 1
        for position in range(1, len(arg)):
            if arg[position] in value_flags:
                if position == len(arg) - 1 and index < len(args):
                    options.append(args[index])
                    index += 1
                break
    return options, args[index:]


def _option_items(
    options: list[str], value_flags: frozenset[str] = _SSH_VALUE_FLAGS
) -> list[tuple[str, str, list[str]]]:
    """``(flag, value, raw tokens)`` for each option group, flag ``""`` for value-less groups."""
    items: list[tuple[str, str, list[str]]] = []
    index = 0
    while index < len(options):
        arg = options[index]
        index += 1
        flag, value, raw = "", "", [arg]
        for position in range(1, len(arg)):
            if arg[position] in value_flags:
                flag = arg[position]
                if position == len(arg) - 1 and index < len(options):
                    value = options[index]
                    raw.append(value)
                    index += 1
                else:
                    value = arg[position + 1 :]
                break
        items.append((flag, value, raw))
    return items


def _is_proxy_option(flag: str, value: str) -> bool:
    if flag == "J":
        return True
    if flag != "o":
        return False
    key = value.split("=", 1)[0].split(None, 1)[0].strip().lower()
    return key in {"proxyjump", "proxycommand"}


def _effective_config(real_ssh: str, args: list[str]) -> dict[str, str]:
    """``ssh -G``'s view of these arguments; empty when ssh cannot say."""
    options, rest = split_ssh_args(args)
    if not rest:
        return {}
    try:
        done = subprocess.run(
            [real_ssh, "-G", *options, rest[0]],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if done.returncode != 0:
        return {}
    config: dict[str, str] = {}
    for line in done.stdout.splitlines():
        key, _sep, value = line.partition(" ")
        config.setdefault(key.lower(), value.strip())
    return config


def _quote_command(argv: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def _escape_percent(text: str) -> str:
    return text.replace("%", "%%")


def _hop_destination(hop: str) -> str:
    """A ProxyJump hop as an ssh destination (``user@host:port`` becomes an ``ssh://`` URI)."""
    if hop.startswith("ssh://"):
        return hop
    user, at, hostport = hop.rpartition("@")
    host, colon, port = hostport.rpartition(":")
    if colon and port.isdigit() and (":" not in host or host.startswith("[")):
        return f"ssh://{user}{at}{host}:{port}"
    return hop


def ssh_argv(real_ssh: str, shim_dir: str, args: list[str]) -> list[str]:
    """The real ssh argv: the helper as ``ProxyCommand``, or the jump chain through the shims."""
    options, rest = split_ssh_args(args)
    flags = "".join(option[1:] for option in options if option.startswith("-"))
    if not rest or "G" in flags or "V" in flags:
        return [real_ssh, *args]
    config = _effective_config(real_ssh, args)
    proxy_command = config.get("proxycommand", "none")
    if proxy_command.lower() != "none":
        # A configured ProxyCommand stays; one that runs `ssh -W` meets these shims on PATH.
        return [real_ssh, *args]
    shim_ssh = os.path.join(shim_dir, "ssh.cmd" if os.name == "nt" else "ssh")
    jump = config.get("proxyjump", "none")
    if jump.lower() != "none":
        hops = [hop for hop in jump.split(",") if hop]
        inner = [_escape_percent(shim_ssh)]
        if len(hops) > 1:
            inner += ["-J", _escape_percent(",".join(hops[:-1]))]
        inner += ["-W", "[%h]:%p", _escape_percent(_hop_destination(hops[-1]))]
    else:
        inner = [
            _escape_percent(sys.executable),
            "-I",
            _escape_percent(os.path.abspath(__file__)),
            _escape_percent(shim_dir),
            "connect",
            "%h",
            "%p",
        ]
    kept: list[str] = []
    for flag, value, raw in _option_items(options):
        if not _is_proxy_option(flag, value):
            kept.extend(raw)
    return [
        real_ssh,
        "-o",
        "ProxyJump=none",
        "-o",
        f"ProxyCommand={_quote_command(inner)}",
        *kept,
        *(["--"] if "--" in args else []),
        *rest,
    ]


def copy_argv(real: str, shim_dir: str, args: list[str], tool: str = "scp") -> list[str]:
    """``scp``/``sftp`` with the ssh launcher as their transport, unless ``-S`` names one."""
    value_flags = _COPY_VALUE_FLAGS.get(tool, _COPY_VALUE_FLAGS["scp"])
    options, _rest = split_ssh_args(args, value_flags)
    if any(flag == "S" for flag, _value, _raw in _option_items(options, value_flags)):
        return [real, *args]
    shim_ssh = os.path.join(shim_dir, "ssh.cmd" if os.name == "nt" else "ssh")
    return [real, "-S", shim_ssh, *args]


def _run(argv: list[str]) -> int:
    if os.name == "nt":
        try:
            return subprocess.call(argv)
        except KeyboardInterrupt:
            return 130
    os.execv(argv[0], argv)
    return 127  # pragma: no cover - execv does not return


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        _say("usage: net_connect.py <shim-dir> connect <host> <port> | ssh|scp|sftp [args]")
        return 2
    shim_dir, tool, args = argv[0], argv[1], argv[2:]
    if tool == "connect":
        if len(args) != 2:
            _say("usage: net_connect.py <shim-dir> connect <host> <port>")
            return 2
        return connect_main(args[0], args[1])
    if tool not in TOOLS:
        _say(f"unknown tool {tool!r}")
        return 2
    real = real_program(tool, shim_dir)
    if not real:
        _say(f"{tool} is not installed on this machine")
        return 127
    if tool == "ssh":
        return _run(ssh_argv(real, shim_dir, args))
    return _run(copy_argv(real, shim_dir, args, tool))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
