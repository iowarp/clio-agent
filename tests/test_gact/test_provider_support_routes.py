"""Provider support restore as the API serves it.

* the provider row says ``support_restoring`` while a restore runs and spells
  out a failed restore (with Install as the retry);
* ``GET /v1/providers/support/restores`` and ``POST /v1/providers/support/restore``;
* the server's startup hook plans and runs the restore, and a restored
  provider's catalog evidence is retired.

The installer is replaced (the suite never installs packages); the planning,
record, restorer and routes are real.
"""

from __future__ import annotations

import asyncio
import importlib.util
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from clio_agent.gact import provider_support_boot, server_boot
from clio_agent.gact.app import build_app
from clio_agent.providers import support_record, support_restore


@pytest.fixture(autouse=True)
def _clean_restorer() -> None:
    support_restore.RESTORER.reset()
    support_record.reset_record_failures()
    yield
    support_restore.RESTORER.reset()


@pytest.fixture
def claude_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """``claude_agent_sdk`` is not importable until ``state['installed']`` flips."""
    state = {"installed": False}
    original = importlib.util.find_spec

    def _find_spec(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "claude_agent_sdk" and not state["installed"]:
            return None
        return original(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", _find_spec)
    return state


def _claude_row(client: TestClient) -> dict[str, Any]:
    body = client.get("/v1/providers/lm").json()
    return next(preset for preset in body["presets"] if preset["id"] == "claude_code")


def _wait_until(predicate: Any, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached before the deadline")
        time.sleep(0.02)


def test_the_claude_code_row_reports_restoring_then_a_plain_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_sdk_missing: dict[str, bool]
) -> None:
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    app = build_app(sessions_path=tmp_path / "s.json")
    step = support_restore.RestoreStep("claude_code", "install", support_restore.SUPPORT_MISSING)
    jobs = support_restore.RESTORER.claim([step])

    with TestClient(app) as client:
        restoring = _claude_row(client)
        assert restoring["status"] == "support_restoring"
        assert restoring["status_message"] == "Restoring Claude Code support after the update…"
        assert restoring["is_authenticated"] is False

        def _offline(_kind: str) -> None:
            raise RuntimeError("could not reach pypi.org")

        support_restore.RESTORER.execute(jobs, install=_offline, update=lambda _k: None)
        failed = _claude_row(client)
        restores = client.get("/v1/providers/support/restores").json()

    assert failed["status"] == "install_required"
    assert "could not restore Claude Code support after the update" in failed["status_message"]
    assert "could not reach pypi.org" in failed["status_message"]
    assert restores["running"] is False
    assert restores["restores"] == [
        {
            "provider_kind": "claude_code",
            "display_name": "Claude Code",
            "action": "install",
            "reason": "support_missing",
            "state": "failed",
            "error": {
                "code": "provider_support_restore_failed",
                "message": "could not reach pypi.org",
            },
            "started_at": jobs[0].started_at,
            "finished_at": jobs[0].finished_at,
        }
    ]


def test_the_retry_route_restores_recorded_support_the_environment_lacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_sdk_missing: dict[str, bool]
) -> None:
    support_record.record_support("claude_code")
    installed: list[str] = []

    def _install(kind: str) -> bool:
        installed.append(kind)
        claude_sdk_missing["installed"] = True
        return True

    monkeypatch.setattr(provider_support_boot, "_install", _install)
    monkeypatch.setenv("CLIO_MODEL_CATALOG", str(tmp_path / "overlay.json"))
    app = build_app(sessions_path=tmp_path / "s.json")

    with TestClient(app) as client:
        accepted = client.post("/v1/providers/support/restore")
        assert accepted.status_code == 202
        _wait_until(lambda: not support_restore.RESTORER.running)
        restores = client.get("/v1/providers/support/restores").json()
        row = _claude_row(client)

    assert installed == ["claude_code"]
    assert [(r["provider_kind"], r["state"]) for r in restores["restores"]] == [
        ("claude_code", "restored")
    ]
    assert row["status"] != "install_required"


def test_the_retry_route_refuses_while_a_restore_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claude_sdk_missing: dict[str, bool]
) -> None:
    support_record.record_support("claude_code")
    gate = threading.Event()
    monkeypatch.setattr(provider_support_boot, "_install", lambda _kind: gate.wait(10))
    app = build_app(sessions_path=tmp_path / "s.json")

    with TestClient(app) as client:
        try:
            assert client.post("/v1/providers/support/restore").status_code == 202
            refused = client.post("/v1/providers/support/restore")
        finally:
            gate.set()
        _wait_until(lambda: not support_restore.RESTORER.running)

    assert refused.status_code == 409
    assert "provider_support_restore_running" in refused.text


def test_an_unwritable_record_is_reported_by_the_restore_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent import user_config_document

    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(user_config_document, "user_config_path", lambda: blocked / "config.yaml")
    support_record.record_support("claude_code")
    app = build_app(sessions_path=tmp_path / "s.json")

    with TestClient(app) as client:
        body = client.get("/v1/providers/support/restores").json()

    assert [(f["provider_kind"], f["reason"]) for f in body["record_failures"]] == [
        ("claude_code", "provider_support_not_recorded")
    ]


def test_server_startup_runs_the_restore_and_retires_catalog_evidence(
    monkeypatch: pytest.MonkeyPatch, claude_sdk_missing: dict[str, bool]
) -> None:
    support_record.record_support("claude_code")
    monkeypatch.setattr(provider_support_boot, "_install", lambda _kind: True)
    retired: list[str] = []
    monkeypatch.setattr(
        "clio_agent.gact.provider_catalog_snapshot.invalidate_provider",
        lambda _app, provider_id: retired.append(provider_id),
    )
    monkeypatch.setattr(
        "clio_agent.gact.routes.health_boot.start_boot_collection", lambda *_a, **_k: None
    )

    async def _boot() -> None:
        app = SimpleNamespace(
            state=SimpleNamespace(
                server_boot=True, arc=object(), mcp_app_loop=asyncio.get_running_loop()
            )
        )
        server_boot.start(app)  # type: ignore[arg-type]
        assert support_restore.RESTORER.job("claude_code").state in {"restoring", "restored"}  # type: ignore[union-attr]
        app.state.provider_support_restore_thread.join(10)
        await asyncio.sleep(0.05)  # let the loop run the thread-safe invalidations

    asyncio.run(_boot())

    assert support_restore.RESTORER.job("claude_code").state == "restored"  # type: ignore[union-attr]
    assert "claude_code" in retired


def test_a_startup_restore_that_cannot_be_planned_never_blocks_boot(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _broken() -> list[support_restore.RestoreStep]:
        raise OSError("config volume is gone")

    monkeypatch.setattr(provider_support_boot, "plan_boot_restore", _broken)

    provider_support_boot.start(SimpleNamespace(state=SimpleNamespace()))  # type: ignore[arg-type]

    assert provider_support_boot.PROVIDER_SUPPORT_RESTORE_NOT_PLANNED in caplog.text
    assert support_restore.RESTORER.jobs() == []
