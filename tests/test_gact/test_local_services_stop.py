"""Stopping this computer's supervised services after the server (``clio stop``).

No real process is started: liveness and the platform's process-tree stop are faked,
while the ownership check, lifecycle lock and receipt rewrite are the controller's own.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.infrastructure import local_services, native_service_platform, node_service
from clio_agent.gact.infrastructure.local_services import LocalService

HOST = socket.gethostname()


def _service(
    base: Path, name: str, *, host: str = HOST, owner: str = "", root: str | None = None
) -> Path:
    directory = base / "services" / host / name
    directory.mkdir(parents=True)
    marker = {"owner": owner or f"owner-{name}", "host": host, "root": root or str(directory)}
    (directory / "owner.json").write_text(json.dumps(marker), encoding="utf-8")
    return directory


@pytest.fixture
def base(tmp_path: Path) -> Path:
    # The controller refuses symlinked roots, so use the resolved form of the temp dir.
    return tmp_path.resolve() / "data"


def test_default_layout_finds_owned_services_and_skips_foreign_ones(base: Path) -> None:
    searxng = _service(base, "searxng")
    _service(base, "vllm", host="other-node")
    _service(base, "copied", root=str(base / "elsewhere" / "copied"))
    (base / "services" / HOST / "no-marker").mkdir()

    found = local_services.local_services(
        service_data_roots=[str(base / "services")], ledger=base / "absent.json"
    )

    assert found == [LocalService(root=str(searxng), owner="owner-searxng")]


def test_ledger_directories_and_local_storage_roots_are_included(base: Path) -> None:
    relocated = _service(base / "custom", "web_search")
    stored = _service(base / "storage", "ollama")
    remote = _service(base / "remote", "vllm")
    ledger = base / "infrastructure.json"
    ledger.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "targets": {
                    "local": {"id": "local", "storage": {"service_data": str(stored.parent.parent)}}
                },
                "services": {
                    "local:web_search": {
                        "target_id": "local",
                        "owned_resources": [{"kind": "directory", "ref": str(relocated)}],
                    },
                    "gpu:vllm": {
                        "target_id": "gpu",
                        "owned_resources": [{"kind": "directory", "ref": str(remote)}],
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    found = local_services.local_services(service_data_roots=[], ledger=ledger)

    assert sorted(service.name for service in found) == ["ollama", "web_search"]


def test_only_running_services_are_stopped_through_the_controller(base: Path) -> None:
    services = [
        LocalService(root=str(base / "services" / HOST / name), owner=f"owner-{name}")
        for name in ("searxng", "idle")
    ]
    requests: list[dict[str, Any]] = []

    outcomes = local_services.stop_local_services(
        services,
        running=lambda service: 4321 if service.name == "searxng" else 0,
        control=lambda request: requests.append(request) or {},
    )

    assert requests == [
        {
            "root": str(base / "services" / HOST / "searxng"),
            "owner": "owner-searxng",
            "action": "stop",
        }
    ]
    assert [(outcome.service.name, outcome.pid, outcome.error) for outcome in outcomes] == [
        ("searxng", 4321, None)
    ]


def test_a_failed_stop_is_reported_and_the_others_still_stop(base: Path) -> None:
    services = [LocalService(root=str(base / name), owner=name) for name in ("broken", "fine")]
    stopped: list[str] = []

    def control(request: dict[str, Any]) -> dict[str, Any]:
        if request["owner"] == "broken":
            raise RuntimeError("Native service children did not stop; data retained")
        stopped.append(request["owner"])
        return {}

    outcomes = local_services.stop_local_services(services, running=lambda _: 7, control=control)

    assert stopped == ["fine"]
    assert outcomes[0].error == "Native service children did not stop; data retained"
    assert outcomes[1].error is None


def test_names_limit_which_services_stop(base: Path) -> None:
    services = [LocalService(root=str(base / name), owner=name) for name in ("searxng", "vllm")]
    requests: list[dict[str, Any]] = []

    local_services.stop_local_services(
        services,
        names={"vllm"},
        running=lambda _: 7,
        control=lambda request: requests.append(request) or {},
    )

    assert [request["owner"] for request in requests] == ["vllm"]


def test_main_is_quiet_and_succeeds_when_nothing_runs(
    base: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _service(base, "searxng")
    monkeypatch.setattr(
        local_services, "default_service_data_roots", lambda: [str(base / "services")]
    )
    monkeypatch.setattr(local_services, "ledger_path", lambda: base / "absent.json")

    assert local_services.main(["stop"]) == 0
    assert capsys.readouterr() == ("", "")


def test_main_reports_one_line_per_service_and_fails_on_an_error(
    base: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ok = LocalService(root=str(base / "searxng"), owner="a")
    bad = LocalService(root=str(base / "vllm"), owner="b")
    monkeypatch.setattr(local_services, "local_services", lambda: [ok, bad])
    monkeypatch.setattr(
        local_services,
        "stop_local_services",
        lambda services, names: [
            local_services.StopOutcome(ok, 11),
            local_services.StopOutcome(bad, 0, "Port busy"),
        ],
    )

    assert local_services.main(["stop"]) == 1
    output = capsys.readouterr()
    assert output.out == "stopped searxng (pid 11)\n"
    assert output.err == "could not stop vllm: Port busy\n"


def test_cli_dispatches_the_services_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.ui.cli_commands import handle_local_command

    seen: list[list[str]] = []
    monkeypatch.setattr(local_services, "main", lambda argv: seen.append(list(argv)) or 0)

    with pytest.raises(SystemExit) as exited:
        handle_local_command(["services", "stop", "searxng"])

    assert exited.value.code == 0
    assert seen == [["stop", "searxng"]]


def test_a_running_service_ends_stopped_with_its_installation_kept(
    base: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _service(base, "searxng")
    receipt = {
        "phase": "running",
        "installed": True,
        "pid": 4321,
        "process_identity": "id-4321",
        "generation": "g1",
        "configuration_revision": "rev",
    }
    (root / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    signalled: list[int] = []
    # test_supervisor registers the standalone helper as clio_native_platform, which
    # platform_helper() prefers; pin the module this test patches.
    monkeypatch.setattr(node_service, "platform_helper", lambda: native_service_platform)
    monkeypatch.setattr(
        node_service, "identity", lambda pid: "" if signalled or pid != 4321 else "id-4321"
    )
    monkeypatch.setattr(native_service_platform, "stop", lambda row: signalled.append(row["pid"]))
    monkeypatch.setattr(
        native_service_platform, "stop_posix", lambda row, alive: signalled.append(row["pid"])
    )
    monkeypatch.setattr(native_service_platform, "wait_launcher_exit", lambda row: None)
    monkeypatch.setattr(
        local_services, "default_service_data_roots", lambda: [str(base / "services")]
    )
    monkeypatch.setattr(local_services, "ledger_path", lambda: base / "absent.json")

    assert local_services.main(["stop"]) == 0
    assert capsys.readouterr().out == "stopped searxng (pid 4321)\n"
    assert signalled == [4321]
    stopped = json.loads((root / "receipt.json").read_text(encoding="utf-8"))
    assert stopped["phase"] == "stopped"
    assert stopped["installed"] is True
    assert stopped["configuration_revision"] == "rev"
    assert (stopped["pid"], stopped["process_identity"]) == (0, "")
    assert (root / "owner.json").is_file()

    # Idempotent: a second stop finds nothing running and changes nothing.
    assert local_services.main(["stop"]) == 0
    assert capsys.readouterr() == ("", "")
    assert signalled == [4321]
