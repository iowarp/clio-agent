"""Judge settings preserve user configuration and resolve independent identities."""

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent import conf
from clio_agent.gact.fleet_settings import (
    FleetUpdate,
    fleet_role_configured,
    fleet_settings,
    judge_route,
    save_fleet_settings,
)
from clio_agent.gact.routes.fleet_settings import register_fleet_settings_routes
from tests._config_layer import read_config, set_config


def test_environment_judge_mapping_and_explicit_inherit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "CLIO_FLEET_JUDGES",
        '{"goals": null, "permissions": {"provider_id": "codex", "model_id": "gpt-6-luna"}}',
    )
    conf.reload()
    assert fleet_role_configured("goals")
    assert not fleet_role_configured("variants")
    assert fleet_settings()["judges"]["permissions"]["provider_id"] == "codex"
    monkeypatch.setenv("CLIO_FLEET_JUDGES", '{"misspelled": null}')
    with pytest.raises(ValueError, match="Fleet judges"):
        fleet_settings()


def test_independent_choices_persist_and_inherit_exact_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    conf.reload()
    set_config("providers.private_key", "private-test-value")
    current = fleet_settings()
    assert current["judges"] == dict.fromkeys(
        ("goals", "variants", "permissions", "memory_extract", "memory_consolidate")
    )
    caller, adapter = object(), object()
    for role in ("goals", "variants", "permissions"):
        assert judge_route(role, caller, adapter) == (caller, adapter)
    saved = save_fleet_settings(
        FleetUpdate(
            revision=current["revision"],
            judges={
                "goals": {"provider_id": "codex", "model_id": "gpt-6-luna"},
                "variants": {"provider_id": "codex", "model_id": "gpt-6.1-sol"},
            },
        )
    )
    assert saved["judges"]["permissions"] is None
    assert saved["judges"]["variants"]["model_id"] == "gpt-6.1-sol"
    assert read_config()["providers"]["private_key"] == "private-test-value"
    assert "private-test-value" not in str(saved)
    conf.reload()
    assert fleet_settings() == saved
    with pytest.raises(ValueError, match="changed"):
        save_fleet_settings(FleetUpdate(revision=current["revision"], judges={"goals": None}))
    reset = save_fleet_settings(FleetUpdate(revision=saved["revision"], judges={"goals": None}))
    assert reset["judges"]["variants"] == saved["judges"]["variants"]
    assert judge_route("goals", caller, adapter) == (caller, adapter)


def test_routes_reject_unknown_roles_providers_and_stale_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    conf.reload()
    app = FastAPI()
    register_fleet_settings_routes(app)
    with TestClient(app) as client:
        current = client.get("/v1/settings/fleet").json()
        for judges in ({"unknown": None}, {"goals": {"provider_id": "missing", "model_id": "m"}}):
            assert (
                client.patch(
                    "/v1/settings/fleet", json={"revision": current["revision"], "judges": judges}
                ).status_code
                == 422
            )
        assert (
            client.patch(
                "/v1/settings/fleet", json={"revision": "stale", "judges": {"goals": None}}
            ).status_code
            == 409
        )
        assert client.get("/v1/settings/fleet").json() == current


def test_explicit_role_uses_selected_provider_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    from clio_agent.gact import fleet_settings as fleet

    caller, adapter, chosen, chosen_adapter = object(), object(), object(), object()
    monkeypatch.setattr(
        fleet,
        "fleet_settings",
        lambda: {"judges": {"goals": {"provider_id": "judge-provider", "model_id": "judge-model"}}},
    )

    def route(provider_id: str, model_id: str) -> tuple[Any, Any]:
        assert (provider_id, model_id) == ("judge-provider", "judge-model")
        return chosen, chosen_adapter

    monkeypatch.setattr(fleet, "model_route", route)
    assert judge_route("goals", caller, adapter) == (chosen, chosen_adapter)


def test_search_model_is_separate_and_survives_legacy_judge_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    conf.reload()
    original = fleet_settings()
    saved = save_fleet_settings(
        FleetUpdate(
            revision=original["revision"],
            judges={},
            search={"provider_id": "codex", "model_id": "gpt-6-luna"},
        )
    )
    assert saved["judges"] == original["judges"]
    assert saved["search"]["model_id"] == "gpt-6-luna"
    legacy = save_fleet_settings(FleetUpdate(revision=saved["revision"], judges={"goals": None}))
    assert legacy["search"] == saved["search"]
    reset = save_fleet_settings(FleetUpdate(revision=legacy["revision"], judges={}, search=None))
    assert reset["search"] is None


def test_spotter_default_is_its_own_slot_and_accepts_a_saved_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import local_server_store as store
    from clio_agent.gact.fleet.spotter_model import default_spotter_ref

    monkeypatch.chdir(tmp_path)
    conf.reload()
    original = fleet_settings()
    assert original["spotter"] is None and default_spotter_ref() == ""
    server = store.add_server(address="http://gpu-node:8001", label="vLLM spotter")
    app = FastAPI()
    register_fleet_settings_routes(app)
    with TestClient(app) as client:
        unknown = client.patch(
            "/v1/settings/fleet",
            json={
                "revision": original["revision"],
                "judges": {},
                "spotter": {"provider_id": "server-missing", "model_id": "m"},
            },
        )
        assert unknown.status_code == 422
        saved = client.patch(
            "/v1/settings/fleet",
            json={
                "revision": original["revision"],
                "judges": {},
                "spotter": {"provider_id": server.id, "model_id": "Qwen3-8B"},
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["spotter"] == {"provider_id": server.id, "model_id": "Qwen3-8B"}
        assert saved.json()["judges"] == original["judges"]
        assert client.get("/v1/settings/fleet").json() == saved.json()
    assert default_spotter_ref() == "server-vllm-spotter/Qwen3-8B"
    # A judges-only update leaves the SPOTTER default alone; null clears it.
    kept = save_fleet_settings(
        FleetUpdate(revision=saved.json()["revision"], judges={"goals": None})
    )
    assert kept["spotter"] == saved.json()["spotter"]
    cleared = save_fleet_settings(FleetUpdate(revision=kept["revision"], judges={}, spotter=None))
    assert cleared["spotter"] is None and default_spotter_ref() == ""
