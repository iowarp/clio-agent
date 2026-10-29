"""B4: a first launch installs the managed packages while the agent already runs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clio_agent.runtime import package_preparation as prep
from clio_agent.runtime.status import IntegrationState


def test_a_running_install_answers_still_preparing(tmp_path: Path) -> None:
    with prep.preparing(tmp_path):
        busy = prep.preparing_result(tmp_path)
        assert busy is not None
        assert busy["status"] == "preparing" and busy["reason"] == prep.REASON_PREPARING
    assert prep.preparing_result(tmp_path) is None
    assert prep.current(tmp_path)["state"] == "ready"


def test_a_failed_install_records_its_typed_reason(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), prep.preparing(tmp_path):
        raise RuntimeError("office download failed")
    record = prep.current(tmp_path)
    assert record["state"] == "failed" and record["reason"] == prep.REASON_FAILED
    assert "office download failed" in record["error"]
    assert prep.preparing_result(tmp_path) is None


def test_an_installer_that_died_is_not_forever_preparing(tmp_path: Path) -> None:
    prep.state_path(tmp_path).write_text(
        json.dumps({"state": "preparing", "pid": 2**22 + 7, "started_at": 0}), encoding="utf-8"
    )
    assert prep.current(tmp_path)["reason"] == prep.REASON_INTERRUPTED
    assert prep.preparing_result(tmp_path) is None


def test_the_health_row_reports_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(prep, "state_path", lambda cache_root=None: tmp_path / prep.STATE_FILE)
    assert prep.probe_managed_packages().state is IntegrationState.SKIPPED
    with prep.preparing(tmp_path):
        row = prep.probe_managed_packages()
        assert row.state is IntegrationState.DEGRADED and not row.required
        assert row.details["reason"] == prep.REASON_PREPARING
    assert prep.probe_managed_packages().state is IntegrationState.READY


def test_document_tools_answer_preparing_instead_of_installing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clio_agent.gact import document_tools

    def must_not_install(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("a tool must not start a second, racing install")

    monkeypatch.setattr(document_tools, "prepare_document_runtime", must_not_install)
    monkeypatch.setattr(document_tools, "_workspace", lambda: tmp_path)
    monkeypatch.setattr(
        document_tools, "preparing_result", lambda: {"status": "preparing", "reason": "x"}
    )
    tool = document_tools.build_prepare_document_runtime_tool()
    assert tool.func()["status"] == "preparing"
