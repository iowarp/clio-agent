"""The saved SSH hosts as an OpenSSH config file, reached as ``${CLIO_SAVED_SSH_CONFIG}``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from clio_agent.gact.agents.resolution import supplied_server_values
from clio_agent.gact.infrastructure import saved_ssh_config
from clio_agent.gact.infrastructure.models import (
    CreateTargetRequest,
    InfrastructureTarget,
    SshRoute,
)
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.plugins.servers import CLIO_SUPPLIED_VARIABLES, SAVED_SSH_CONFIG_VAR


def _ssh(target_id: str, label: str, **route: Any) -> InfrastructureTarget:
    return InfrastructureTarget(id=target_id, label=label, kind="ssh", ssh=SshRoute(**route))


def _blocks(text: str) -> list[str]:
    return [block.strip() for block in text.split("\n\n")[1:]]


def test_each_ssh_host_is_one_host_block_with_its_route() -> None:
    text = saved_ssh_config.render(
        [
            InfrastructureTarget(id="local", label="This computer", kind="local"),
            _ssh("lab", "Lab GPU box", host="gpu.example.org", user="ana", port=2222),
            _ssh(
                "edge",
                "Edge",
                host="10.0.0.5",
                identity_file="~/.ssh/id_lab",
                jump_hosts=["bastion"],
            ),
        ]
    )

    assert text.startswith("# Written by CLIO")
    assert _blocks(text) == [
        "Host lab-gpu-box\n    HostName gpu.example.org\n    User ana\n    Port 2222",
        "Host edge\n    HostName 10.0.0.5\n    Port 22\n"
        "    IdentityFile ~/.ssh/id_lab\n    ProxyJump bastion",
    ]


def test_aliases_are_unique_slugs_and_fall_back_to_the_id() -> None:
    text = saved_ssh_config.render(
        [
            _ssh("a", "Cluster", host="one.example.org"),
            _ssh("b", "cluster", host="two.example.org"),
            _ssh("node-7", "!!!", host="three.example.org"),
            _ssh("profile-only", "Profile", profile="saved-profile"),
        ]
    )

    hosts = [line for line in text.splitlines() if line.startswith("Host ")]
    assert hosts == ["Host cluster", "Host cluster-2", "Host node-7", "Host profile"]
    assert "HostName saved-profile" in text


def test_a_host_without_an_address_is_left_out_and_nothing_secret_is_written() -> None:
    text = saved_ssh_config.render(
        [_ssh("empty", "Empty"), _ssh("lab", "Lab", host="lab.example.org", user="ana")]
    )

    assert "Host empty" not in text
    assert "Host lab" in text
    for word in ("Password", "IdentityFile", "ProxyJump", "IdentitiesOnly"):
        assert word not in text


def test_the_store_rewrites_the_file_whenever_its_hosts_change(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infrastructure.json")
    path = store.saved_ssh_config_path()

    assert path is not None
    assert path == tmp_path / saved_ssh_config.FILE_NAME
    assert path.is_file()
    assert "Host " not in path.read_text(encoding="utf-8")

    created = store.create_target(
        CreateTargetRequest(
            label="Lab", kind="ssh", ssh=SshRoute(host="lab.example.org", user="ana")
        )
    )
    assert "Host lab\n    HostName lab.example.org\n    User ana" in path.read_text(
        encoding="utf-8"
    )

    store.delete_target(created.id)
    assert "Host lab" not in path.read_text(encoding="utf-8")

    path.unlink()
    InfrastructureStore(tmp_path / "infrastructure.json")
    assert path.is_file()


def test_a_memory_only_store_has_no_file() -> None:
    assert InfrastructureStore(None).saved_ssh_config_path() is None


def test_a_plugin_server_reaches_the_file_as_a_supplied_variable(tmp_path: Path) -> None:
    store = InfrastructureStore(tmp_path / "infrastructure.json")
    app = SimpleNamespace(state=SimpleNamespace(infrastructure_store=store))
    rows = {
        "hosts": {
            "spec": {
                "command": "server",
                "env": {"SAVED_HOSTS": "${CLIO_SAVED_SSH_CONFIG:-}"},
            }
        }
    }

    assert SAVED_SSH_CONFIG_VAR in CLIO_SUPPLIED_VARIABLES
    assert supplied_server_values(app, rows, workspace_root=None) == {
        SAVED_SSH_CONFIG_VAR: str(store.saved_ssh_config_path())
    }
    unreferenced = {"hosts": {"spec": {"command": "server", "env": {}}}}
    assert supplied_server_values(app, unreferenced, workspace_root=None) == {}
    no_store = SimpleNamespace(state=SimpleNamespace())
    assert supplied_server_values(no_store, rows, workspace_root=None) == {}
