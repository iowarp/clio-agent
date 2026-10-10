"""F019: a daemon that crashed under the attach is named, with its log, by the probe error."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.arc import clio_core_attach
from clio_agent.arc.runtime_crash import crash_record_path


def _store() -> Any:
    future = SimpleNamespace(done=lambda: False)
    client = SimpleNamespace(AsyncTagQuery=lambda *_: future)
    return SimpleNamespace(
        _client=client,
        _HEALTH_PROBE_NAME="probe",
        _cte=SimpleNamespace(PoolQuery=SimpleNamespace(Dynamic=lambda: None)),
        _gate=SimpleNamespace(port=9413),
        _config_path="/state/cte.yaml",
    )


def _unanswered(monkeypatch: pytest.MonkeyPatch, state: Path) -> None:
    monkeypatch.setenv("CLIO_RUNTIME_STATE_DIR", str(state))
    monkeypatch.setattr(
        "clio_agent.arc.daemon_progress.wait_while_progressing",
        lambda *_, **__: SimpleNamespace(done=False, reason="no_progress", waited_s=10.0),
    )


def test_probe_timeout_names_the_recorded_daemon_crash_and_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unanswered(monkeypatch, tmp_path)
    log = tmp_path / "clio-runtime.log"
    crash_record_path(tmp_path).write_text(
        json.dumps(
            {
                "pid": 176727,
                "exit_code": -11,
                "exit_code_hex": "0xFFFFFFF5",
                "crashed_at": "2026-10-08T01:34:00+00:00",
                "log_tail": "",
                "log_path": str(log),
            }
        )
    )
    released: list[bool] = []
    with pytest.raises(clio_core_attach.ClioCoreAttachError) as caught:
        clio_core_attach.verify_post_attach(_store(), on_failure=lambda: released.append(True))
    message = str(caught.value)
    assert caught.value.degradation_reason == clio_core_attach.CLIO_CORE_POST_ATTACH_PROBE_TIMEOUT
    assert "CRASHED with exit status 0xFFFFFFF5" in message
    assert f"full log: {log}" in message
    assert released == [True]


def test_probe_timeout_without_a_crash_record_keeps_its_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unanswered(monkeypatch, tmp_path)
    with pytest.raises(clio_core_attach.ClioCoreAttachError) as caught:
        clio_core_attach.verify_post_attach(_store(), on_failure=lambda: None)
    assert "CRASHED" not in str(caught.value)
    assert "made no progress" in str(caught.value)
