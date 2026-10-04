"""Provenance activation is verified, namespace-owned, restart-bound and backend-independent."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent import conf
from clio_agent.gact.infrastructure import provenance_connections as connections
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.gact.routes.infrastructure_provenance import (
    register_infrastructure_provenance_routes,
)
from clio_agent.provenance_config import attention_capture_enabled
from clio_agent.user_config_document import read_document, user_config_path, write_document


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLIO_AGENT_HOME", str(tmp_path / "agent-home"))
    monkeypatch.delenv("CLIO_USER_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    conf.reload()
    write_document(user_config_path(), {"lm": {"model": "retained"}})
    conf.reload()
    return tmp_path


def record(root: Path, backend: str = "flowcept") -> Any:
    (root / "captures").mkdir(exist_ok=True)
    path = root / "settings.yaml"
    path.write_text("mq: {password: private-value}", encoding="utf-8")
    return connections.ProvenanceConnectionInput(
        service_id=backend,
        label=backend,
        url="http://127.0.0.1:8380",
        settings_path=str(path) if backend == "flowcept" else "",
        attention_files_dir=str(root / "captures") if backend == "flowcept" else "",
        capture_attention=backend == "flowcept",
    ).record()


def verified(row: Any) -> Any:
    return row.model_copy(
        update={
            "verification": {
                "revision": connections.connection_revision(row),
                "write_readback": True,
            }
        }
    )


def test_activation_keeps_live_configuration_and_other_backend_until_restart(
    isolated: Path,
) -> None:
    assert not attention_capture_enabled()
    cmf = verified(record(isolated, "cmf"))
    assert connections.activate_connection(cmf)["restart_required"]
    flowcept = verified(record(isolated))
    assert connections.activate_connection(flowcept)["restart_required"]
    document = read_document(user_config_path())
    assert document["lm"] == {"model": "retained"}
    assert document["provenance"]["artifacts"]["cmf"]["server_url"] == cmf.url
    assert document["provenance"]["agentic"]["flowcept"]["persistence_owner"] == "collector"
    assert document["provenance"]["agentic"]["providers"] == ["jsonl", "flowcept"]
    assert not attention_capture_enabled()  # no live cache/SDK mutation
    conf.reload()  # next-start configuration supports both the enabled flag and local files
    assert attention_capture_enabled()
    assert conf.store().file_value("provenance.attention.files_dir") == str(isolated / "captures")
    connections.disconnect_connection(flowcept)
    assert not connections.connection_selected(flowcept)
    assert connections.connection_selected(cmf)
    assert flowcept.configuration["settings_path"] and (isolated / "settings.yaml").is_file()


def test_settings_change_invalidates_verification_and_activation(isolated: Path) -> None:
    row = verified(record(isolated))
    (isolated / "settings.yaml").write_text("mq: {password: changed}", encoding="utf-8")
    with pytest.raises(ValueError, match="Verify"):
        connections.activate_connection(row)
    assert read_document(user_config_path()) == {"lm": {"model": "retained"}}


def test_attention_directory_must_exist_on_connected_host(isolated: Path) -> None:
    row = record(isolated)
    row.configuration["attention_files_dir"] = str(isolated / "missing")
    with pytest.raises(ValueError, match="existing readable attention folder"):
        connections.connection_revision(row)


def test_capture_settings_are_part_of_selected_connection_identity(isolated: Path) -> None:
    row = verified(record(isolated))
    connections.activate_connection(row)
    assert connections.connection_selected(row)
    changed = row.model_copy(deep=True)
    changed.configuration["capture_attention"] = "false"
    assert not connections.connection_selected(changed)
    changed.configuration["capture_attention"] = "true"
    changed.configuration["attention_files_dir"] = str(isolated / "elsewhere")
    assert not connections.connection_selected(changed)


def test_activation_preserves_pending_provider_configuration(isolated: Path) -> None:
    write_document(
        user_config_path(), {"provenance": {"agentic": {"providers": ["jsonl", "otel"]}}}
    )
    # The running process still holds the old config; next-start changes must be merged fresh.
    connections.activate_connection(verified(record(isolated)))
    assert read_document(user_config_path())["provenance"]["agentic"]["providers"] == [
        "jsonl",
        "otel",
        "flowcept",
    ]


@pytest.mark.parametrize("replacement", [None, "http://changed:8380"])
def test_probe_cannot_resurrect_or_overwrite_a_changed_connection(
    isolated: Path, monkeypatch: pytest.MonkeyPatch, replacement: str | None
) -> None:
    import clio_agent.gact.routes.infrastructure_provenance as routes

    app = FastAPI()
    store = InfrastructureStore(isolated / "infrastructure.json")
    app.state.infrastructure_store = store
    register_infrastructure_provenance_routes(app)
    row = store.put_connection(record(isolated, "cmf"))

    def probe(current: Any) -> Any:
        if replacement:
            store.put_connection(current.model_copy(update={"url": replacement}))
        else:
            store.delete_connection(current.id)
        return verified(current)

    monkeypatch.setattr(routes, "verify_connection", probe)
    response = TestClient(app).post(f"/v1/infrastructure/provenance/connections/{row.id}/verify")
    assert response.status_code == 409
    current = store.connection(row.id)
    assert (
        current is None
        if replacement is None
        else current is not None and current.url == replacement
    )


def test_migrates_legacy_boolean_attention_without_discarding_it(isolated: Path) -> None:
    write_document(user_config_path(), {"provenance": {"attention": False}})
    conf.reload()
    connections.activate_connection(verified(record(isolated)))
    conf.reload()
    assert attention_capture_enabled()


def test_workspace_override_refuses_save_and_preserves_user_file(isolated: Path) -> None:
    from clio_agent import paths

    write_document(
        paths.workspace_config_path(isolated, "config.yaml"),
        {
            "provenance": {"artifacts": {"provider": "native"}},
        },
    )
    before = read_document(user_config_path())
    with pytest.raises(ValueError, match="workspace configuration"):
        connections.activate_connection(verified(record(isolated, "cmf")))
    assert read_document(user_config_path()) == before


def test_probe_isolated_and_private_diagnostics_never_returned(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = record(isolated)
    calls: list[dict[str, Any]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        assert argv[-1] == "clio_agent.gact.infrastructure.provenance_probe"
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "write_readback": True,
                    "probe_id": "new-probe",
                    "password": "private-value",
                }
            ),
            "private diagnostics",
        )

    monkeypatch.setattr(connections.subprocess, "run", run)
    result = connections.verify_connection(row)
    assert result.verification["probe_id"] == "new-probe"
    assert "private" not in result.model_dump_json()
    assert "private-value" not in calls[0]["input"]
    monkeypatch.setattr(
        connections.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            [], 1, "private-value", "private diagnostics"
        ),
    )
    with pytest.raises(ValueError, match="failed") as error:
        connections.verify_connection(row)
    assert "private" not in str(error.value)


def test_routes_deduplicate_invalidate_failed_probe_and_never_own_lifecycle(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import clio_agent.gact.routes.infrastructure_provenance as routes

    app = FastAPI()
    app.state.infrastructure_store = InfrastructureStore(isolated / "infrastructure.json")
    app.state.semantic_trace_backend = SimpleNamespace(reader=lambda _: None)
    register_infrastructure_provenance_routes(app)
    client = TestClient(app)
    base = "/v1/infrastructure/provenance/connections"
    body = {"service_id": "cmf", "label": "Lab CMF", "url": "http://cmf.example:8380"}
    first = client.post(base, json=body).json()
    assert client.post(base, json=body).json()["id"] == first["id"]
    assert not first["managed"] and not first["verified"] and not first["active"]
    target = f"{base}/{first['id']}"
    assert client.post(target + "/use").status_code == 409
    monkeypatch.setattr(routes, "verify_connection", verified)
    assert client.post(target + "/verify").json()["verified"]
    assert client.post(target + "/use").json()["restart_required"]
    assert client.get(base).json()["connections"][0]["selected"]
    assert client.delete(target).status_code == 409
    assert client.post(target + "/disconnect").status_code == 200

    def fail(_: Any) -> Any:
        raise ValueError("Verification failed")

    monkeypatch.setattr(routes, "verify_connection", fail)
    assert client.post(target + "/verify").status_code == 409
    assert not client.get(base).json()["connections"][0]["verified"]
    assert client.delete(target).status_code == 204
    assert read_document(user_config_path())["provenance"]["artifacts"]["provider"] == "native"


def test_generic_connection_controls_cannot_bypass_provenance_verification(isolated: Path) -> None:
    from clio_agent.gact.app import build_app

    app = build_app(agent=None, sessions_path=isolated / "sessions.json")
    row = app.state.infrastructure_store.put_connection(record(isolated, "cmf"))
    client = TestClient(app)
    base = "/v1/infrastructure/service-connections"
    body = {"service_id": "cmf", "label": "Unverified", "url": "http://cmf:8380"}
    assert client.post(base, json=body).status_code == 409
    assert client.put(f"{base}/{row.id}", json=body).status_code == 409
    assert client.delete(f"{base}/{row.id}").status_code == 409
    assert client.post(f"{base}/{row.id}/check").status_code == 409
    assert app.state.infrastructure_store.connection(row.id) == row
