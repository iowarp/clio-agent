"""The doctor finds the clio-core daemon the way the ARC store does, not from one env var.

It used to read the port only from ``$CLIO_ARC_STORE_CONFIG`` and the pidfile and log only
from ``~/.clio``. A config chosen in the config file, a per-workspace config, the
per-install default port, a daemon started by another CLIO (its record wins: first config
wins), and the per-host state dir were all missed, so the doctor probed a port nothing
listened on and reported a live daemon as down.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent import conf
from clio_agent.arc import clio_core_effective_runtime as effective
from clio_agent.runtime.status import IntegrationState, RuntimeProbe


def _cte_yaml(path: Path, port: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"networking:\n  port: {port}\n", encoding="utf-8")
    return path


def _store(
    tmp_path: Path, config_yaml: str = "", env: dict[str, str] | None = None
) -> conf.ConfigStore:
    cwd = tmp_path / "cwd"
    if config_yaml:
        cfg = cwd / ".clio" / "config.yaml"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(config_yaml, encoding="utf-8")
    cwd.mkdir(parents=True, exist_ok=True)
    return conf.ConfigStore(home=tmp_path / "home", cwd=cwd, env=env or {})


def _probe(
    tmp_path: Path, store: conf.ConfigStore, listening: set[int], **kwargs: object
) -> RuntimeProbe:
    return RuntimeProbe(
        env=kwargs.pop("env", {}),  # type: ignore[arg-type]
        config_store=store,
        module_checker=lambda name: name == "iowarp_core",
        port_checker=lambda port: port in listening,
        **kwargs,  # type: ignore[arg-type]
    )


def _record(state_dir: Path, config_path: Path) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "clio-runtime.version").write_text(
        json.dumps(
            {
                "iowarp_core_version": "2.2.1",
                "daemon_binary": "clio_run",
                "config_path": str(config_path),
            }
        ),
        encoding="utf-8",
    )


def test_config_file_store_config_sets_the_port(tmp_path: Path) -> None:
    cfg = _cte_yaml(tmp_path / "chosen" / "cte.yaml", 23456)
    store = _store(tmp_path, f"arc:\n  store_config: {cfg.as_posix()}\n")

    status = _probe(tmp_path, store, {23456}, clio_runtime_dir=tmp_path / "state").probe_clio_core()

    assert status.state == IntegrationState.READY
    assert status.details["port"] == 23456
    assert status.details["config_source"] == "arc.store_config"
    assert status.details["config_path"] == str(cfg)


def test_the_running_daemons_record_wins_over_the_requested_config(tmp_path: Path) -> None:
    """Another CLIO spawned the daemon with its own config: that port is the live one."""
    requested = _cte_yaml(tmp_path / "mine" / "cte.yaml", 23456)
    theirs = _cte_yaml(tmp_path / "theirs" / "cte.yaml", 24567)
    state = tmp_path / "state"
    _record(state, theirs)
    (state / "clio-runtime.pid").write_text("4242 1.0", encoding="utf-8")
    (state / "clio-runtime.log").write_text("FATAL: daemon log line\n", encoding="utf-8")
    store = _store(tmp_path, env={"CLIO_ARC_STORE_CONFIG": str(requested)})

    status = _probe(tmp_path, store, {24567}, clio_runtime_dir=state).probe_clio_core()

    assert status.state == IntegrationState.READY
    assert status.details["port"] == 24567
    assert status.details["config_source"] == "daemon_record"
    assert status.details["config_path"] == str(theirs)
    assert status.details["daemon_pid"] == 4242


def test_a_record_whose_config_is_gone_falls_back_to_the_requested_config(tmp_path: Path) -> None:
    requested = _cte_yaml(tmp_path / "mine" / "cte.yaml", 23456)
    state = tmp_path / "state"
    _record(state, tmp_path / "deleted" / "cte.yaml")
    store = _store(tmp_path, env={"CLIO_ARC_STORE_CONFIG": str(requested)})

    status = _probe(tmp_path, store, set(), clio_runtime_dir=state).probe_clio_core()

    assert status.details["port"] == 23456
    assert status.details["config_source"] == "arc.store_config"
    assert status.details["reason"] == "clio_core_daemon_not_listening"


def test_the_workspace_config_is_used_when_none_is_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws"
    cfg = _cte_yaml(workspace / ".clio" / "core" / "cte.yaml", 25678)
    monkeypatch.chdir(workspace)

    status = _probe(
        tmp_path, _store(tmp_path), {25678}, clio_runtime_dir=tmp_path / "state"
    ).probe_clio_core()

    assert status.details["port"] == 25678
    assert status.details["config_source"] == "workspace"
    assert Path(status.details["config_path"]).resolve() == cfg.resolve()


def test_an_unseeded_default_config_reports_the_per_install_port_without_seeding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cte_dir = tmp_path / "cte"
    monkeypatch.setenv("CLIO_ARC_CTE_DIR", str(cte_dir))
    monkeypatch.setenv("CLIO_CORE_PORT", "26789")
    monkeypatch.chdir(tmp_path)

    status = _probe(
        tmp_path, _store(tmp_path), set(), clio_runtime_dir=tmp_path / "state"
    ).probe_clio_core()

    assert status.details["port"] == 26789
    assert status.details["config_source"] == "default"
    assert not cte_dir.exists()  # the doctor observes; it never seeds the default config


def test_the_state_dir_defaults_to_the_runtimes_per_host_dir(tmp_path: Path) -> None:
    """Without an explicit dir the pidfile and log come from $CLIO_RUNTIME_STATE_DIR."""
    state = tmp_path / "host-state"
    state.mkdir()
    (state / "clio-runtime.log").write_text("FATAL: from the per-host log\n", encoding="utf-8")
    cfg = _cte_yaml(tmp_path / "cte.yaml", 23456)
    env = {"CLIO_RUNTIME_STATE_DIR": str(state), "CLIO_ARC_STORE_CONFIG": str(cfg)}

    status = _probe(tmp_path, _store(tmp_path, env=env), set(), env=env).probe_clio_core()

    assert status.details["log_path"] == str(state / "clio-runtime.log")
    assert any("per-host" in line for line in status.details["log_tail"])


def test_runtime_state_path_never_creates_the_directory(tmp_path: Path) -> None:
    target = tmp_path / "not-created"
    assert effective.runtime_state_path({"CLIO_RUNTIME_STATE_DIR": str(target)}) == target
    assert not target.exists()


@pytest.mark.parametrize(
    ("content", "expected"),
    [("", (None, None)), ("not-a-pid", (None, None)), ("4242", (4242, None))],
)
def test_read_daemon_pid_tolerates_missing_and_malformed_pidfiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str, expected: tuple[object, object]
) -> None:
    from clio_agent.arc import pid_identity

    monkeypatch.setattr(pid_identity, "pid_alive", lambda pid, recorded: None)
    if content:
        (tmp_path / "clio-runtime.pid").write_text(content, encoding="utf-8")
    assert effective.read_daemon_pid(tmp_path) == expected


def test_the_backend_follows_the_config_file_before_the_env(tmp_path: Path) -> None:
    """``arc.store`` in the config file is what the store builds, env notwithstanding;
    clio-core is always required."""
    store = _store(tmp_path, "arc:\n  store: cte\n", env={"CLIO_ARC_STORE": "other"})
    probe = _probe(
        tmp_path, store, set(), env={"CLIO_ARC_STORE": "other"}, clio_runtime_dir=tmp_path / "s"
    )

    status = probe.probe_clio_core()

    assert status.details["arc_backend"] == "cte"
    assert status.required is True
    assert "config:arc.store" in (status.config_source or "")


def test_an_empty_backend_env_means_the_default_like_the_store(tmp_path: Path) -> None:
    store = _store(tmp_path, env={"CLIO_ARC_STORE": " "})
    probe = _probe(
        tmp_path, store, set(), env={"CLIO_ARC_STORE": " "}, clio_runtime_dir=tmp_path / "s"
    )

    assert probe._arc_backend() == ("cte", "default:cte")
