"""SSH through the fence (RL37): the CONNECT helper, the ssh/scp/sftp launcher and its shims."""

from __future__ import annotations

import base64
import os
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from clio_agent.runtime import fence_ssh, net_connect, sandbox
from clio_agent.runtime import execution_environment as execution

HELPER = str(Path(net_connect.__file__).resolve())


def _fake_http_proxy(status: bytes) -> tuple[int, list[bytes]]:
    """One-shot proxy: records the CONNECT head, answers ``status``, then echoes until EOF."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    seen: list[bytes] = []

    def serve() -> None:
        conn, _ = server.accept()
        with conn, server:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(1024)
                if not chunk:
                    return
                head += chunk
            seen.append(head)
            conn.sendall(b"HTTP/1.1 " + status + b"\r\n\r\n")
            if status.startswith(b"200"):
                while chunk := conn.recv(1024):
                    conn.sendall(chunk)

    threading.Thread(target=serve, daemon=True).start()
    return server.getsockname()[1], seen


def _fake_socks_proxy(reply: int) -> tuple[int, list[bytes]]:
    """One-shot SOCKS5 proxy (no auth) answering the CONNECT with ``reply``."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    seen: list[bytes] = []

    def serve() -> None:
        conn, _ = server.accept()
        with conn, server:
            greeting = conn.recv(3)
            seen.append(greeting)
            conn.sendall(b"\x05\x00")
            request = conn.recv(1024)
            seen.append(request)
            conn.sendall(bytes([5, reply, 0, 1, 127, 0, 0, 1, 0, 22]))
            if reply == 0:
                conn.sendall(conn.recv(1024))

    threading.Thread(target=serve, daemon=True).start()
    return server.getsockname()[1], seen


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in net_connect.PROXY_VARIABLES:
        monkeypatch.delenv(key, raising=False)


# --------------------------------------------------------------------------- #
# The CONNECT helper
# --------------------------------------------------------------------------- #


def test_the_proxy_variables_are_read_in_order() -> None:
    assert net_connect.proxy_from_environment({}) == ""
    env = {"http_proxy": "http://h:1", "ALL_PROXY": "socks5h://s:2"}
    assert net_connect.proxy_from_environment(env) == "socks5h://s:2"
    env["HTTPS_PROXY"] = "127.0.0.1:3"
    assert net_connect.proxy_from_environment(env) == "http://127.0.0.1:3"


def test_an_http_tunnel_carries_bytes_and_names_the_ssh_port() -> None:
    port, seen = _fake_http_proxy(b"200 Connection established")
    sock, first = net_connect.open_tunnel("example.org", 22, f"http://u%40x:p@127.0.0.1:{port}")
    with sock:
        sock.sendall(b"SSH-2.0-test\r\n")
        assert first + sock.recv(64) == b"SSH-2.0-test\r\n"
    head = seen[0].decode()
    assert head.startswith("CONNECT example.org:22 HTTP/1.1\r\n")
    token = base64.b64encode(b"u@x:p").decode()
    assert f"Proxy-Authorization: Basic {token}" in head


def test_an_ipv6_target_is_bracketed() -> None:
    port, seen = _fake_http_proxy(b"200 OK")
    sock, _first = net_connect.open_tunnel("2001:db8::1", 22, f"http://127.0.0.1:{port}")
    sock.close()
    assert seen[0].startswith(b"CONNECT [2001:db8::1]:22 HTTP/1.1")


def test_a_refused_connect_reports_the_policy(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    port, seen = _fake_http_proxy(b"403 Forbidden")
    monkeypatch.setenv("HTTPS_PROXY", f"http://127.0.0.1:{port}")
    assert net_connect.main(["/nowhere", "connect", "blocked.example", "22"]) == 1
    assert seen[0].startswith(b"CONNECT blocked.example:22 ")
    err = capsys.readouterr().err
    assert "the sandbox network policy refused blocked.example:22" in err
    assert "403 Forbidden" in err


def test_a_socks_proxy_is_spoken_to_in_socks5() -> None:
    port, seen = _fake_socks_proxy(0)
    sock, _first = net_connect.open_tunnel("example.org", 22, f"socks5h://127.0.0.1:{port}")
    with sock:
        sock.sendall(b"hi")
        assert sock.recv(2) == b"hi"
    assert seen[1] == b"\x05\x01\x00\x03\x0bexample.org\x00\x16"


def test_a_socks_ruleset_refusal_is_a_policy_refusal() -> None:
    port, _seen = _fake_socks_proxy(2)
    with pytest.raises(net_connect.ProxyRefused) as caught:
        net_connect.open_tunnel("example.org", 22, f"socks5://127.0.0.1:{port}")
    assert caught.value.status == 403


def test_the_helper_joins_stdio_to_the_tunnel() -> None:
    port, seen = _fake_http_proxy(b"200 Connection established")
    env = {**os.environ, "HTTPS_PROXY": f"http://127.0.0.1:{port}"}
    done = subprocess.run(
        [sys.executable, "-I", HELPER, "/nowhere", "connect", "example.org", "22"],
        input=b"payload over the tunnel",
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == b"payload over the tunnel"
    assert seen[0].startswith(b"CONNECT example.org:22 ")


# --------------------------------------------------------------------------- #
# The ssh / scp / sftp launcher
# --------------------------------------------------------------------------- #


def test_ssh_arguments_split_like_getopt() -> None:
    split = net_connect.split_ssh_args
    assert split(["-p", "2222", "-vT", "host", "ls", "-l"]) == (
        ["-p", "2222", "-vT"],
        ["host", "ls", "-l"],
    )
    assert split(["-ouser=x", "-iKEY", "host"]) == (["-ouser=x", "-iKEY"], ["host"])
    assert split(["-x", "--", "host", "cmd"]) == (["-x"], ["host", "cmd"])


def _config(monkeypatch: pytest.MonkeyPatch, config: dict[str, str]) -> None:
    monkeypatch.setattr(net_connect, "_effective_config", lambda _ssh, _args: config)


def test_a_plain_ssh_gets_the_helper_as_its_proxy_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _config(monkeypatch, {"hostname": "example.org"})
    argv = net_connect.ssh_argv("/usr/bin/ssh", "/shims", ["-p", "2222", "host", "hostname"])
    assert argv[:4] == ["/usr/bin/ssh", "-o", "ProxyJump=none", "-o"]
    command = argv[4]
    assert command.startswith("ProxyCommand=")
    assert "connect %h %p" in command
    assert HELPER in command or HELPER.replace("%", "%%") in command
    assert argv[5:] == ["-p", "2222", "host", "hostname"]


def test_a_configured_proxy_command_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _config(monkeypatch, {"proxycommand": "nc %h %p"})
    assert net_connect.ssh_argv("/usr/bin/ssh", "/shims", ["host"]) == ["/usr/bin/ssh", "host"]


def test_version_and_config_dumps_pass_through(monkeypatch: pytest.MonkeyPatch) -> None:
    _config(monkeypatch, {})
    assert net_connect.ssh_argv("/s", "/d", ["-V"]) == ["/s", "-V"]
    assert net_connect.ssh_argv("/s", "/d", ["-G", "host"]) == ["/s", "-G", "host"]


def test_a_jump_chain_crosses_the_proxy_at_every_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    _config(monkeypatch, {"proxyjump": "first,admin@second:2200"})
    argv = net_connect.ssh_argv(
        "/usr/bin/ssh", "/shims", ["-J", "first,admin@second:2200", "-o", "ProxyJump=x", "target"]
    )
    shim_ssh = os.path.join("/shims", "ssh.cmd" if os.name == "nt" else "ssh")
    command = argv[4].removeprefix("ProxyCommand=")
    assert shim_ssh in command
    assert "-J first" in command
    assert "-W '[%h]:%p'" in command or '-W "[%h]:%p"' in command or "-W [%h]:%p" in command
    assert command.endswith("ssh://admin@second:2200")
    assert argv[5:] == ["target"], "the user's -J and ProxyJump are replaced by the chain"


@pytest.mark.parametrize(
    ("tool", "args", "expected_prefix"),
    [
        ("scp", ["-p", "file", "host:"], ["/bin/scp", "-S"]),
        ("scp", ["-S", "/my/ssh", "file", "host:"], ["/bin/scp", "-S", "/my/ssh"]),
        ("sftp", ["-s", "/srv", "host"], ["/bin/sftp", "-S"]),
        ("sftp", ["-S/my/ssh", "host"], ["/bin/sftp", "-S/my/ssh"]),
    ],
)
def test_copies_use_the_ssh_launcher_unless_told_otherwise(
    tool: str, args: list[str], expected_prefix: list[str]
) -> None:
    argv = net_connect.copy_argv(f"/bin/{tool}", "/shims", args, tool)
    assert argv[: len(expected_prefix)] == expected_prefix
    if expected_prefix[-1] == "-S":
        assert argv[2] == os.path.join("/shims", "ssh.cmd" if os.name == "nt" else "ssh")
        assert argv[3:] == args


def test_the_real_program_skips_the_shim_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shims, real = tmp_path / "shims", tmp_path / "real"
    for directory in (shims, real):
        directory.mkdir()
        target = directory / ("ssh.exe" if os.name == "nt" else "ssh")
        target.write_text("", encoding="utf-8")
        target.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(shims), str(real)]))
    found = net_connect.real_program("ssh", str(shims))
    assert Path(found).parent == real


@pytest.mark.skipif(os.name == "nt" or not shutil.which("ssh"), reason="needs POSIX OpenSSH")
def test_real_ssh_reaches_the_proxy_on_port_22_and_reports_a_refusal(tmp_path: Path) -> None:
    """The real ssh runs the helper, which asks the proxy for host:22 and surfaces its 403."""
    port, seen = _fake_http_proxy(b"403 Forbidden")
    shims = fence_ssh.shim_directory()
    assert shims is not None
    env = {
        **os.environ,
        "HTTPS_PROXY": f"http://127.0.0.1:{port}",
        "PATH": os.pathsep.join([str(shims), os.environ.get("PATH", "")]),
    }
    done = subprocess.run(
        ["ssh", "-F", "/dev/null", "-o", "BatchMode=yes", "refused.example", "true"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert done.returncode == 255
    assert seen and seen[0].startswith(b"CONNECT refused.example:22 ")
    assert "the sandbox network policy refused refused.example:22" in done.stderr


# --------------------------------------------------------------------------- #
# The shims on a fenced shell's PATH
# --------------------------------------------------------------------------- #


def test_no_fence_means_no_ssh_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "fence_enforced", lambda: False)
    assert fence_ssh.fence_ssh_environment({"PATH": "/usr/bin"}) == {}


def test_a_host_without_ssh_gets_no_shims(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "fence_enforced", lambda: True)
    monkeypatch.setattr(fence_ssh.shutil, "which", lambda _name, *, path: None)
    assert fence_ssh.fence_ssh_environment({"PATH": "/usr/bin"}) == {}


def test_a_fenced_shell_finds_the_shims_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "fence_enforced", lambda: True)
    monkeypatch.setattr(fence_ssh.shutil, "which", lambda _name, *, path: "/usr/bin/ssh")
    monkeypatch.delenv("GIT_SSH", raising=False)
    overlay = fence_ssh.fence_ssh_environment(
        {"PATH": "/usr/bin", "CLIO_EXECUTION_PATH": "/managed"}
    )
    shims = Path(overlay["PATH"].split(os.pathsep)[0])
    assert overlay["PATH"] == os.pathsep.join([str(shims), "/usr/bin"])
    assert overlay["CLIO_EXECUTION_PATH"] == os.pathsep.join([str(shims), "/managed"])
    suffix = ".cmd" if os.name == "nt" else ""
    assert overlay["GIT_SSH"] == str(shims / f"ssh{suffix}")
    for tool in net_connect.TOOLS:
        shim = shims / f"{tool}{suffix}"
        text = shim.read_text(encoding="utf-8")
        assert HELPER in text and tool in text and sys.executable in text
        if os.name != "nt":
            assert os.access(shim, os.X_OK)


def test_a_chosen_git_ssh_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "fence_enforced", lambda: True)
    monkeypatch.setattr(fence_ssh.shutil, "which", lambda _name, *, path: "/usr/bin/ssh")
    overlay = fence_ssh.fence_ssh_environment({"PATH": "/usr/bin", "GIT_SSH": "/my/ssh"})
    assert "GIT_SSH" not in overlay


def test_the_shell_environment_carries_the_shims_inside_a_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox, "fence_enforced", lambda: True)
    monkeypatch.setattr(execution, "installed_github_cli", lambda: None)
    real_which = shutil.which
    monkeypatch.setattr(
        fence_ssh.shutil,
        "which",
        lambda name, *, path=None: "/usr/bin/ssh" if name == "ssh" else real_which(name, path=path),
    )
    environment = execution.shell_environment(tmp_path)
    shims = fence_ssh.shim_directory()
    assert shims is not None
    assert environment["PATH"].split(os.pathsep)[0] == str(shims)
    assert environment["CLIO_EXECUTION_PATH"].split(os.pathsep)[0] == str(shims)
