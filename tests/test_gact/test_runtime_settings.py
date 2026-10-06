"""Real user-file persistence, provenance and rejection boundaries for Settings."""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clio_agent import conf
from clio_agent.arc import history_mode
from clio_agent.gact.routes.runtime_settings import register_runtime_settings_routes
from clio_agent.gact.runtime_settings import (
    RuntimeSettingsError,
    UpdateRuntimeSettings,
    get_runtime_settings,
    update_runtime_settings,
)
from clio_agent.user_config_document import user_config_path
from tests._config_layer import read_config, set_config


def test_save_and_inherit_use_runtime_store_preserving_private_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLIO_MAX_LM_CALL_S", "55")
    conf.reload()
    set_config("providers.servers", [{"id": "private", "api_key": "do-not-expose"}])
    set_config("limits.unrelated", 4321)
    initial = get_runtime_settings()
    row = next(r for r in initial.settings if r.key == "limits.lm_call_s")
    assert row.value == 55 and row.source == "environment"
    assert "do-not-expose" not in initial.model_dump_json()
    saved = update_runtime_settings(
        UpdateRuntimeSettings(
            revision=initial.revision,
            changes={"limits.lm_call_s": 88.5, "runtime.capture_reasoning": False},
        )
    )
    assert conf.resolve("limits.lm_call_s", env="CLIO_MAX_LM_CALL_S", default=1800.0) == 88.5
    assert read_config()["limits"]["unrelated"] == 4321
    assert read_config()["providers"]["servers"][0]["api_key"] == "do-not-expose"
    assert next(r for r in saved.settings if r.key == "limits.lm_call_s").source == "user"
    inherited = update_runtime_settings(
        UpdateRuntimeSettings(
            revision=saved.revision,
            changes={"limits.lm_call_s": None},
        )
    )
    assert "lm_call_s" not in read_config()["limits"]
    assert read_config()["runtime"]["capture_reasoning"] is False
    row = next(r for r in inherited.settings if r.key == "limits.lm_call_s")
    assert row.value == 55 and row.source == "environment" and not row.has_user_value


def test_invalid_or_stale_update_preserves_entire_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    conf.reload()
    initial = get_runtime_settings()
    original = user_config_path().read_bytes()
    for invalid in (False, float("nan"), float("inf"), 0, -1):
        with pytest.raises(RuntimeSettingsError):
            update_runtime_settings(
                UpdateRuntimeSettings(
                    revision=initial.revision,
                    changes={
                        "runtime.capture_reasoning": False,
                        "limits.lm_call_s": invalid,
                    },
                )
            )
        assert user_config_path().read_bytes() == original
    with pytest.raises(RuntimeSettingsError):
        update_runtime_settings(
            UpdateRuntimeSettings(revision=initial.revision, changes={"lm.api_key": 1})
        )
    with pytest.raises(RuntimeSettingsError):
        update_runtime_settings(
            UpdateRuntimeSettings(revision=initial.revision, changes={"autocompact.pct": 1.1})
        )
    with pytest.raises(RuntimeSettingsError):
        update_runtime_settings(
            UpdateRuntimeSettings(
                revision=initial.revision, changes={"limits.lm_transient_retries": 1.5}
            )
        )
    set_config("providers.unrelated", "external edit")
    changed = user_config_path().read_bytes()
    with pytest.raises(RuntimeSettingsError) as error:
        update_runtime_settings(
            UpdateRuntimeSettings(revision=initial.revision, changes={"limits.lm_call_s": 90})
        )
    assert error.value.status_code == 409
    assert user_config_path().read_bytes() == changed


def test_workspace_and_legacy_overrides_are_visible_and_locked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLIO_MAX_LM_CALL_S", "15")
    set_config("limits.lm_call_s", 20)
    legacy = tmp_path / ".clio" / "config.yaml"
    legacy.parent.mkdir()
    legacy.write_text("limits:\n  lm_call_s: 30\n", encoding="utf-8")
    initial = get_runtime_settings()
    row = next(r for r in initial.settings if r.key == "limits.lm_call_s")
    assert row.value == 30 and row.source == "workspace" and not row.editable
    with pytest.raises(RuntimeSettingsError):
        update_runtime_settings(
            UpdateRuntimeSettings(revision=initial.revision, changes={row.key: 40})
        )
    workspace = tmp_path / ".clio-agent" / "shared" / "config.yaml"
    workspace.parent.mkdir(parents=True)
    workspace.write_text("limits:\n  lm_call_s: 50\n", encoding="utf-8")
    assert next(r for r in get_runtime_settings().settings if r.key == row.key).value == 50
    workspace.write_text("limits: replaced\n", encoding="utf-8")
    current = next(r for r in get_runtime_settings().settings if r.key == row.key)
    assert current.value == 15 and current.source == "environment" and current.editable


def test_routes_validate_types_and_do_not_expose_invalid_yaml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    conf.reload()
    app = FastAPI()
    register_runtime_settings_routes(app)
    with TestClient(app) as client:
        initial = client.get("/v1/settings/runtime")
        assert initial.status_code == 200
        revision = initial.json()["revision"]
        invalid = client.patch(
            "/v1/settings/runtime",
            json={"revision": revision, "changes": {"limits.lm_call_s": "55"}},
        )
        assert invalid.status_code == 422
        saved = client.patch(
            "/v1/settings/runtime",
            json={"revision": revision, "changes": {"limits.lm_transient_retries": 3}},
        )
        assert saved.status_code == 200
        assert read_config()["limits"]["lm_transient_retries"] == 3
        assert (
            client.patch(
                "/v1/settings/runtime",
                json={"revision": revision, "changes": {"limits.lm_call_s": 55}},
            ).status_code
            == 409
        )
        user_config_path().write_text("secret-token: [private-value", encoding="utf-8")
        broken = client.get("/v1/settings/runtime")
        assert broken.status_code == 500
        assert "private-value" not in broken.text and "secret-token" not in broken.text


def test_history_mode_requires_transcript_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(history_mode, "active", lambda: True)
    conf.reload()
    initial = get_runtime_settings()
    row = next(r for r in initial.settings if r.key == "transcript.file")
    assert not row.editable and row.reason
    with pytest.raises(RuntimeSettingsError, match="required"):
        update_runtime_settings(
            UpdateRuntimeSettings(revision=initial.revision, changes={row.key: False})
        )
    assert read_config().get("transcript", {}).get("file", True) is True
