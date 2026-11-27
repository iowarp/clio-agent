"""Declared raw TCP endpoints forwarded from inside the Linux fence through CLIO's proxy."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from clio_agent.runtime import fence_forward, net_connect

HELPER = str(Path(net_connect.__file__).resolve())


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (*net_connect.PROXY_VARIABLES, net_connect.RELAY_VARIABLE):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv(fence_forward.FORWARD_ENV, raising=False)


def _resolver(table: dict[str, list[str]]):
    def getaddrinfo(host, port, *args, **kwargs):
        if host not in table:
            raise socket.gaierror("unknown")
        out = []
        for address in table[host]:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            out.append((family, socket.SOCK_STREAM, 6, "", (address, port)))
        return out

    return getaddrinfo


def test_a_forward_pair_names_where_to_listen_and_where_to_go() -> None:
    assert net_connect.parse_forward("127.0.1.1:6817=ares:6817") == (
        "127.0.1.1",
        6817,
        "ares",
        6817,
    )
    assert net_connect.parse_forward("[::1]:22=[fd00::5]:22") == ("::1", 22, "fd00::5", 22)
    for bad in ("ares:6817", "127.0.0.1:x=ares:1", "127.0.0.1:1=ares:70000", "=ares:1"):
        with pytest.raises(ValueError):
            net_connect.parse_forward(bad)


def test_a_loopback_name_is_forwarded_where_it_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    table = {"ctl": ["127.0.1.1", "172.20.1.1"], "ares": ["172.20.1.1"], "db": ["10.0.0.6"]}
    monkeypatch.setattr(fence_forward.socket, "getaddrinfo", _resolver(table))
    endpoints = [("ctl", 6817), ("ares", 6817), ("ares", 6819), ("db", 6817), ("10.0.0.7", 1)]
    pairs, remap, env = fence_forward.forward_plan(endpoints)
    assert pairs == [
        "127.0.1.1:6817=ctl:6817",
        "127.0.0.2:6817=ares:6817",
        "127.0.0.2:6819=ares:6819",
        "127.0.0.3:6817=db:6817",
        "127.0.0.4:1=10.0.0.7:1",
    ]
    # A name the client looks up is renamed for it; an address can only be published.
    assert remap == {"ares": "127.0.0.2", "db": "127.0.0.3"}
    assert env == {
        fence_forward.PUBLISHED_ENV: "ares:6817=127.0.0.2:6817,ares:6819=127.0.0.2:6819,"
        "db:6817=127.0.0.3:6817,10.0.0.7:1=127.0.0.4:1"
    }


def test_an_unresolvable_host_is_still_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fence_forward.socket, "getaddrinfo", _resolver({}))
    pairs, remap, env = fence_forward.forward_plan([("slurm.example", 6817)])
    assert pairs == ["127.0.0.2:6817=slurm.example:6817"]
    assert remap == {"slurm.example": "127.0.0.2"}
    assert env == {fence_forward.PUBLISHED_ENV: "slurm.example:6817=127.0.0.2:6817"}


def test_the_child_hosts_file_names_only_the_forwarder() -> None:
    original = (
        "127.0.0.1 localhost\n"
        "127.0.1.1 ares.ares.local\n"
        "172.20.1.1 ares ares.ares.local # controller\n"
        "172.20.1.9 db\n"
    )
    text = net_connect._hosts_text(original, {"ares": "127.0.0.2", "db": "127.0.0.3"})
    assert text.splitlines() == [
        "127.0.0.2 ares",
        "127.0.0.3 db",
        "127.0.0.1 localhost",
        "127.0.1.1 ares.ares.local",
        "172.20.1.1 ares.ares.local # controller",
    ]


def test_endpoints_come_from_the_setting_and_bad_ones_are_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(fence_forward.FORWARD_ENV, "ares:6817, nope, ares:6817,[fd00::1]:6819")
    assert fence_forward.configured_endpoints() == [("ares", 6817), ("fd00::1", 6819)]


def test_only_the_linux_proxy_fence_with_endpoints_is_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fence_forward, "configured_endpoints", lambda: [("ctl", 6817)])
    monkeypatch.setattr(fence_forward.socket, "getaddrinfo", _resolver({}))
    assert fence_forward.wrap_forwarding("srv", ["a"], {"HTTPS_PROXY": "x"}) == ("srv", ["a"], {})

    relay = {net_connect.RELAY_VARIABLE: net_connect.RELAY_AUTHORITY}
    command, args, env = fence_forward.wrap_forwarding("srv", ["a"], relay)
    assert command == sys.executable
    assert args == [
        "-I",
        HELPER,
        "-",
        "forward",
        "--hosts=ctl=127.0.0.2",
        "127.0.0.2:6817=ctl:6817",
        "--",
        "srv",
        "a",
    ]
    assert env == {fence_forward.PUBLISHED_ENV: "ctl:6817=127.0.0.2:6817"}

    monkeypatch.setattr(fence_forward, "configured_endpoints", lambda: [])
    assert fence_forward.wrap_forwarding("srv", ["a"], relay) == ("srv", ["a"], {})


def _relay_proxy() -> tuple[int, list[bytes]]:
    """Accept the relay tunnel, then the inner CONNECT, then echo what the client sends."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    seen: list[bytes] = []

    def head(conn: socket.socket) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(1)
            if not chunk:
                return data
            data += chunk
        return data

    def serve() -> None:
        conn, _ = server.accept()
        with conn, server:
            seen.append(head(conn))
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            seen.append(head(conn))
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            while chunk := conn.recv(1024):
                conn.sendall(chunk)

    threading.Thread(target=serve, daemon=True).start()
    return server.getsockname()[1], seen


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the forwarder is Linux-only")
@pytest.mark.parametrize("by_name", [False, True], ids=["address", "renamed-host"])
def test_a_raw_client_reaches_the_endpoint_through_the_relay(by_name: bool) -> None:
    proxy_port, seen = _relay_proxy()
    listen_port = _free_port()
    target = "slurmctl.example" if by_name else "127.0.0.1"
    bind = "127.0.0.2" if by_name else "127.0.0.1"
    hosts = [f"--hosts=slurmctl.example={bind}"] if by_name else []
    client = (
        "import socket,sys,time\n"
        "for _ in range(50):\n"
        "    try:\n"
        f"        s = socket.create_connection(('{target}', {listen_port}), timeout=5); break\n"
        "    except OSError: time.sleep(0.1)\n"
        "s.sendall(b'ping'); print(s.recv(16).decode())\n"
    )
    env = {
        **os.environ,
        "HTTPS_PROXY": f"http://127.0.0.1:{proxy_port}",
        net_connect.RELAY_VARIABLE: net_connect.RELAY_AUTHORITY,
    }
    done = subprocess.run(
        [
            sys.executable,
            "-I",
            HELPER,
            "-",
            "forward",
            *hosts,
            f"{bind}:{listen_port}=slurmctl.example:6817",
            "--",
            sys.executable,
            "-c",
            client,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if by_name and "keep their addresses" in done.stderr:
        pytest.skip("this kernel does not allow an unprivileged user namespace")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ping"
    assert seen[0].startswith(f"CONNECT {net_connect.RELAY_AUTHORITY} ".encode())
    assert seen[1].startswith(b"CONNECT slurmctl.example:6817 HTTP/1.1\r\n")


def test_without_a_proxy_the_program_runs_unforwarded() -> None:
    done = subprocess.run(
        [sys.executable, "-I", HELPER, "-", "forward", "127.0.0.1:1=x:1", "--", sys.executable]
        + ["-c", "print('ran')"],
        env={k: v for k, v in os.environ.items() if k not in net_connect.PROXY_VARIABLES},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ran"
