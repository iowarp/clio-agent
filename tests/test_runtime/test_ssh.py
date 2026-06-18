"""SshRunner command construction (local vs remote) + the LD_LIBRARY_PATH gotcha."""

from __future__ import annotations

from clio_agent.runtime.ssh import SshRunner


def test_local_command_is_env_prefixed_argv():
    r = SshRunner(ld_library_path="/lib:/bin")
    cmd = r.build_command("localhost", ["clio_run", "start"], {"CLIO_SERVER_CONF": "/c.yaml"})
    assert cmd[0] == "env"
    assert "CLIO_SERVER_CONF=/c.yaml" in cmd
    assert "LD_LIBRARY_PATH=/lib:/bin" in cmd  # mandatory, injected
    assert cmd[-2:] == ["clio_run", "start"]
    assert "ssh" not in cmd  # local host -> no ssh hop


def test_remote_command_goes_over_ssh_with_user_and_opts():
    r = SshRunner(user="deployer", ld_library_path="/lib", opts=["-o", "BatchMode=yes"])
    cmd = r.build_command("node7", ["clio_run", "start", "--induct"], {"X": "1"})
    assert cmd[0] == "ssh"
    assert "deployer@node7" in cmd
    assert "-o" in cmd and "BatchMode=yes" in cmd
    # the remote payload is a single shell string carrying env + argv
    payload = cmd[-1]
    assert payload.startswith("env ")
    assert "X=1" in payload and "LD_LIBRARY_PATH=/lib" in payload
    assert "clio_run start --induct" in payload


def test_force_ssh_routes_even_localhost_over_ssh():
    r = SshRunner(force_ssh=True)
    assert not r.is_local("localhost")
    assert r.build_command("localhost", ["echo", "hi"])[0] == "ssh"


def test_is_local_recognizes_this_host():
    import socket

    r = SshRunner()
    assert r.is_local("localhost")
    assert r.is_local("127.0.0.1")
    assert r.is_local(socket.gethostname())
    assert not r.is_local("some-remote-node")
