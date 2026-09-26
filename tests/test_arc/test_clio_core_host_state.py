"""One clio-core daemon per MACHINE: its state is keyed by host, even on a shared home.

A cluster's login and compute nodes mount the same NFS home. Before the host
key, a login node's daemon pidfile, lock, client registry and CTE storage in
``~/.clio`` / ``~/.local/share/clio-agent/cte`` were read by a compute node's
server as its own (live on ares-comp-11: ``WaitForLocalServer ... Cannot
connect``). Each test runs two "machines" over ONE home directory.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clio_agent import conf, paths
from clio_agent.arc import clio_core_config, runtime_stop, storage


@pytest.fixture
def shared_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """One home directory (the NFS mount) and no state-dir override."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.delenv("CLIO_RUNTIME_STATE_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(paths, "user_data_dir", lambda: home / ".local" / "share" / "clio-agent")
    monkeypatch.setattr(conf, "_STORE", conf.ConfigStore(home=home, cwd=tmp_path / "cwd", env={}))
    monkeypatch.setattr(storage, "_client_registered", False)
    monkeypatch.setattr(runtime_stop, "_runtime_shutdown_requested", False)
    return home


def _on(monkeypatch: pytest.MonkeyPatch, hostname: str) -> None:
    monkeypatch.setattr(clio_core_config.socket, "gethostname", lambda: hostname)


@pytest.mark.parametrize(
    ("hostname", "key"),
    [
        ("ares", "ares"),
        ("ares.ares.local", "ares"),
        ("ares-comp-11.cluster.example.edu", "ares-comp-11"),
        ("DESKTOP-AB12CD", "desktop-ab12cd"),
        ("weird name!", "weird-name"),
        ("a-!b", "a--b"),
        ("", "localhost"),
        (".", "localhost"),
    ],
)
def test_host_key_is_the_short_lowercase_hostname(
    monkeypatch: pytest.MonkeyPatch, hostname: str, key: str
) -> None:
    _on(monkeypatch, hostname)
    assert clio_core_config.host_key() == key


def test_two_hosts_on_one_home_keep_separate_runtime_state(
    shared_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, "ares.ares.local")
    login = clio_core_config.runtime_state_dir()
    _on(monkeypatch, "ares-comp-11")
    compute = clio_core_config.runtime_state_dir()

    assert login == shared_home / ".clio" / "hosts" / "ares"
    assert compute == shared_home / ".clio" / "hosts" / "ares-comp-11"
    assert login.is_dir() and compute.is_dir()


def test_a_login_nodes_daemon_files_are_invisible_to_a_compute_node(
    shared_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The login node's server registered itself and recorded its daemon.
    _on(monkeypatch, "ares")
    storage._register_client()
    storage._daemon_pidfile().write_text(f"{os.getpid()} ", encoding="utf-8")
    login_lock = storage.runtime_state_dir() / "clio-runtime.lock"
    with storage._runtime_spawn_lock():
        pass
    assert storage._live_client_pids() == [os.getpid()]
    monkeypatch.setattr(storage, "_client_registered", False)

    # On the compute node that pid means nothing: no client, no daemon, own lock.
    _on(monkeypatch, "ares-comp-11")
    assert storage._live_client_pids() == []
    assert not storage._daemon_pidfile().exists()
    with storage._runtime_spawn_lock():
        assert (storage.runtime_state_dir() / "clio-runtime.lock").exists()
    assert storage.runtime_state_dir() / "clio-runtime.lock" != login_lock
    # ...and the login node's records are untouched by the compute node.
    _on(monkeypatch, "ares")
    assert storage._live_client_pids() == [os.getpid()]
    assert storage._daemon_pidfile().exists()
    storage._deregister_client()


def test_each_host_seeds_its_own_cte_config_and_storage(
    shared_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, "ares")
    login_cfg = Path(clio_core_config.default_cte_config_path())
    _on(monkeypatch, "ares-comp-11")
    compute_cfg = Path(clio_core_config.default_cte_config_path())

    data = shared_home / ".local" / "share" / "clio-agent" / "cte" / "hosts"
    assert login_cfg == data / "ares" / "cte.yaml"
    assert compute_cfg == data / "ares-comp-11" / "cte.yaml"
    # The file tier, metadata log and runtime conf dir are the host's own.
    compute_text = compute_cfg.read_text(encoding="utf-8")
    assert (data / "ares-comp-11" / "storage.bin").as_posix() in compute_text
    assert (data / "ares-comp-11" / "metadata.log").as_posix() in compute_text
    assert (data / "ares-comp-11" / "conf").as_posix() in compute_text
    assert "/hosts/ares/" not in compute_text
    # The same host always resolves to the same config (one daemon per machine).
    assert Path(clio_core_config.default_cte_config_path()) == compute_cfg


def test_an_explicit_state_dir_or_cte_dir_is_used_exactly(
    shared_home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _on(monkeypatch, "ares-comp-11")
    monkeypatch.setenv("CLIO_RUNTIME_STATE_DIR", str(tmp_path / "private"))
    assert clio_core_config.runtime_state_dir() == tmp_path / "private"

    monkeypatch.setattr(
        conf,
        "_STORE",
        conf.ConfigStore(
            home=shared_home,
            cwd=tmp_path / "cwd",
            env={"CLIO_ARC_CTE_DIR": str(tmp_path / "scratch" / "cte")},
        ),
    )
    assert clio_core_config._default_cte_dir() == tmp_path / "scratch" / "cte"
