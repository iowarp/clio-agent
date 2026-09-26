"""clio-core daemon/client compatibility: the version gate and first-config-wins adoption.

One clio-core daemon runs per machine. A client of a different iowarp-core version
must be refused loudly BEFORE any native call (live evidence: a mismatched attach loops
or kills the shared daemon), and a second CLIO asking for a different config attaches
with the running daemon's config plus a typed, visible adoption note. Binding-free:
the daemon's liveness is faked; the version record and configs are real files.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clio_agent.arc import clio_core_attach, runtime_stop, storage
from clio_agent.arc.clio_core_attach import ClioCoreAttachPhase, attach_state_snapshot
from clio_agent.arc.clio_core_daemon_version import (
    CLIO_CORE_CONFIG_ADOPTED_FROM_DAEMON,
    CLIO_CORE_DAEMON_CONFIG_UNKNOWN,
    CLIO_CORE_DAEMON_VERSION_UNKNOWN,
    CLIO_CORE_VERSION_MISMATCH,
    ClioCoreDaemonIncompatibleError,
    config_adoption_snapshot,
    installed_iowarp_core_version,
    read_daemon_version,
    record_daemon_version,
    refuse_if_incompatible_daemon,
    reset_config_adoption,
    resolve_effective_config,
    running_daemon_config,
)
from clio_agent.arc.init_degradation import (
    arc_init_degradation_snapshot,
    classify_init_failure,
    reset_arc_init_degradation,
)
from clio_agent.runtime.clio_core_health import (
    probe_clio_core_config_adoption,
    probe_clio_core_health,
)
from clio_agent.runtime.status import IntegrationState

_CTE_CONFIG = """\
networking:
  port: {port}
compose:
  - mod_name: clio_bdev
    pool_name: "ram::chi_default_bdev"
    pool_id: "301.0"
    bdev_type: ram
    capacity: "{ram}"
  - mod_name: clio_cte_core
    pool_name: cte_main
    pool_id: "512.0"
    storage:
      - path: "{tier}"
        bdev_type: "file"
        capacity_limit: "8GB"
        score: 1.0
"""


def _cte_config(path: Path, *, ram: str = "1GB", port: int = 4321, tier: str = "s.bin") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_CTE_CONFIG.format(port=port, ram=ram, tier=tier), encoding="utf-8")
    return str(path)


def _write_record(state: Path, *, version: str, config_path: str) -> None:
    (state / "clio-runtime.version").write_text(
        '{"iowarp_core_version": "%s", "daemon_binary": "/opt/iowarp/bin/clio_run", '
        '"config_path": "%s"}' % (version, config_path.replace("\\", "\\\\")),
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _fresh_records():
    reset_config_adoption()
    reset_arc_init_degradation()
    clio_core_attach.reset_attach_state()
    yield
    reset_config_adoption()
    reset_arc_init_degradation()
    clio_core_attach.reset_attach_state()


@pytest.fixture
def machine(monkeypatch, tmp_path) -> Path:
    """An isolated per-machine state dir with a daemon already listening."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("CLIO_RUNTIME_STATE_DIR", str(state))
    monkeypatch.setattr(storage, "_client_registered", False)
    monkeypatch.setattr(runtime_stop, "_runtime_shutdown_requested", False)
    monkeypatch.setattr(storage, "_runtime_alive", lambda _port: True)

    def no_spawn(*_a: object, **_k: object) -> None:  # pragma: no cover - asserts never called
        raise AssertionError("spawned a second daemon while one was running")

    monkeypatch.setattr(storage, "_spawn_runtime_daemon", no_spawn)
    return state


# ---- the record --------------------------------------------------------------


def test_record_round_trips_this_processs_own_version(tmp_path):
    record_daemon_version(tmp_path, daemon_binary="/bin/clio_run", config_path="/c/cte.yaml")
    record = read_daemon_version(tmp_path)
    assert record is not None
    assert record.iowarp_core_version == installed_iowarp_core_version()
    assert record.iowarp_core_version  # iowarp-core is a core dependency: never blank
    assert (record.daemon_binary, record.config_path) == ("/bin/clio_run", "/c/cte.yaml")


@pytest.mark.parametrize("body", ["not json {{{", '{"iowarp_core_version": "2.1.0"}'])
def test_malformed_record_reads_as_absent(tmp_path, body):
    (tmp_path / "clio-runtime.version").write_text(body, encoding="utf-8")
    assert read_daemon_version(tmp_path) is None


# ---- version gate --------------------------------------------------------------


def test_version_mismatch_refuses_typed_naming_both_versions_and_the_fix(tmp_path):
    _write_record(tmp_path, version="2.2.1", config_path="")
    ran: list[bool] = []

    with pytest.raises(ClioCoreDaemonIncompatibleError) as info:
        refuse_if_incompatible_daemon(
            tmp_path, client_version="2.1.0", on_failure=lambda: ran.append(True)
        )

    err = info.value
    assert err.degradation_reason == CLIO_CORE_VERSION_MISMATCH
    assert classify_init_failure(err) == CLIO_CORE_VERSION_MISMATCH
    assert err.details["daemon_version"] == "2.2.1"
    assert err.details["client_version"] == "2.1.0"
    message = str(err)
    assert "2.2.1" in message and "2.1.0" in message
    assert "Stop every CLIO on this machine" in message  # the fix is named
    assert ran == [True]  # deregistered before raising


def test_missing_record_refuses_typed_as_unknown(tmp_path):
    with pytest.raises(ClioCoreDaemonIncompatibleError) as info:
        refuse_if_incompatible_daemon(tmp_path, client_version="2.1.0")
    assert info.value.degradation_reason == CLIO_CORE_DAEMON_VERSION_UNKNOWN
    assert classify_init_failure(info.value) == CLIO_CORE_DAEMON_VERSION_UNKNOWN
    assert "2.1.0" in str(info.value)


def test_same_version_passes_the_gate(tmp_path):
    _write_record(tmp_path, version="2.1.0", config_path="")
    record = refuse_if_incompatible_daemon(tmp_path, client_version="2.1.0")
    assert record.iowarp_core_version == "2.1.0"


def test_ensure_daemon_refuses_a_mismatched_daemon_before_any_attach(machine, tmp_path):
    """Through the real connect-or-spawn seam: refused, deregistered, no second daemon."""
    cfg = _cte_config(tmp_path / "cte.yaml")
    _write_record(machine, version="0.0.0-other", config_path=cfg)

    with pytest.raises(ClioCoreDaemonIncompatibleError) as info:
        storage._ensure_runtime_daemon(object(), cfg, "error")

    assert info.value.details["daemon_version"] == "0.0.0-other"
    assert info.value.details["client_version"] == installed_iowarp_core_version()
    assert os.getpid() not in storage._live_client_pids()  # holds no vote in the refcount
    assert storage._client_registered is False


def test_ensure_daemon_attaches_to_a_same_version_daemon(machine, tmp_path):
    cfg = _cte_config(tmp_path / "cte.yaml")
    record_daemon_version(machine, daemon_binary="/bin/clio_run", config_path=cfg)

    assert storage._ensure_runtime_daemon(object(), cfg, "error") == cfg
    assert os.getpid() in storage._live_client_pids()
    assert config_adoption_snapshot() is None  # same config: no note


def test_mismatch_surfaces_typed_in_health_rows(machine, tmp_path, monkeypatch):
    """make_arc_store degrades LOUDLY: the typed reason reaches both clio-core rows."""
    import sys  # noqa: PLC0415
    import types  # noqa: PLC0415

    from clio_agent.arc import clio_core_file_capacity  # noqa: PLC0415

    monkeypatch.setitem(sys.modules, "iowarp_core", types.ModuleType("iowarp_core"))
    monkeypatch.setitem(sys.modules, "clio_cte_core_ext", types.ModuleType("clio_cte_core_ext"))
    monkeypatch.setattr(storage.ClioCoreStore, "_initialized", False)
    monkeypatch.setattr(clio_core_file_capacity, "preflight_clio_core_config", lambda *a, **k: None)
    cfg = _cte_config(tmp_path / "cte.yaml")
    _write_record(machine, version="0.0.0-other", config_path=cfg)

    store = storage.make_arc_store(backend="cte", data_dir=tmp_path / "fb", config_path=cfg)

    assert isinstance(store, storage.LocalFSStore)
    degraded = arc_init_degradation_snapshot()
    assert degraded is not None and degraded.reason == CLIO_CORE_VERSION_MISMATCH
    snap = attach_state_snapshot()
    assert (snap.phase, snap.reason) == (
        ClioCoreAttachPhase.UNAVAILABLE,
        CLIO_CORE_VERSION_MISMATCH,
    )
    assert "0.0.0-other" in snap.error


# ---- first config wins ------------------------------------------------------------


def test_second_config_adopts_the_first_with_a_typed_note(machine, tmp_path):
    first = _cte_config(tmp_path / "a" / "cte.yaml", ram="8GB", tier="a.bin")
    second = _cte_config(tmp_path / "b" / "cte.yaml", ram="4GB", tier="b.bin")
    record_daemon_version(machine, daemon_binary="/bin/clio_run", config_path=first)

    effective = storage._ensure_runtime_daemon(object(), second, "error")

    assert effective == first  # the running daemon's config wins, not the request
    adoption = config_adoption_snapshot()
    assert adoption is not None
    assert (adoption.requested_config_path, adoption.effective_config_path) == (second, first)
    assert adoption.diffs["ram_bdev_capacity"] == {"requested": "4GB", "effective": "8GB"}
    assert "cte_storage_tiers" in adoption.diffs

    rows = probe_clio_core_config_adoption()
    assert len(rows) == 1
    row = rows[0]
    assert row.name == "clio_core_config_adoption"
    assert row.state is IntegrationState.DEGRADED
    assert row.required is False  # a note, never a 503
    assert row.details["reason"] == CLIO_CORE_CONFIG_ADOPTED_FROM_DAEMON
    assert "ram_bdev_capacity" in row.summary
    assert "clio_core_config_adoption" in [r.name for r in probe_clio_core_health()]


def test_adoption_finds_the_daemon_on_its_own_port(machine, tmp_path, monkeypatch):
    """A request declaring another port still reaches the ONE running daemon."""
    first = _cte_config(tmp_path / "a" / "cte.yaml", port=4321)
    second = _cte_config(tmp_path / "b" / "cte.yaml", port=5555)
    record_daemon_version(machine, daemon_binary="/bin/clio_run", config_path=first)
    probed: list[int] = []
    monkeypatch.setattr(storage, "_runtime_alive", lambda port: probed.append(port) or port == 4321)

    assert storage._ensure_runtime_daemon(object(), second, "error") == first
    assert probed == [4321]
    assert running_daemon_config(machine, second) == first


def test_same_config_records_no_adoption_and_no_row(tmp_path):
    cfg = _cte_config(tmp_path / "cte.yaml")
    _write_record(tmp_path, version="2.1.0", config_path=cfg)
    assert resolve_effective_config(tmp_path, cfg, client_version="2.1.0") == cfg
    assert config_adoption_snapshot() is None
    assert probe_clio_core_config_adoption() == []


def test_missing_daemon_config_refuses_typed(tmp_path):
    _write_record(tmp_path, version="2.1.0", config_path=str(tmp_path / "gone.yaml"))
    with pytest.raises(ClioCoreDaemonIncompatibleError) as info:
        resolve_effective_config(tmp_path, "req.yaml", client_version="2.1.0")
    assert info.value.degradation_reason == CLIO_CORE_DAEMON_CONFIG_UNKNOWN


def test_spawn_path_records_version_and_config(monkeypatch, tmp_path):
    """The spawner writes the record the gate reads (binding-free spawn)."""
    import subprocess  # noqa: PLC0415

    monkeypatch.setenv("CLIO_RUNTIME_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(storage, "_runtime_launcher_path", lambda _core: "/opt/bin/clio_run")
    monkeypatch.setattr(storage, "watch_daemon_process", lambda *a, **k: None)

    class _Proc:
        pid = os.getpid()

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _Proc())

    class _Core:
        @staticmethod
        def get_lib_dir() -> str:
            return str(tmp_path)

    storage._spawn_runtime_daemon(_Core(), "/cfg/cte.yaml", "error")

    record = read_daemon_version(tmp_path)
    assert record is not None
    assert record.config_path == "/cfg/cte.yaml"
    assert record.daemon_binary == "/opt/bin/clio_run"
    assert record.iowarp_core_version == installed_iowarp_core_version()
