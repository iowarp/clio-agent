"""The durable record of installed provider support (``providers.installed_support``).

Every test runs against the suite's isolated ``CLIO_USER_DIR`` (conftest), so
the ``config.yaml`` read and written here is a scratch file, never a real one.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
import yaml

from clio_agent import user_config_document
from clio_agent.providers import support_record


@pytest.fixture(autouse=True)
def _clean_failures() -> None:
    support_record.reset_record_failures()


def _config() -> dict[str, Any]:
    path = user_config_document.user_config_path()
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def test_recording_an_install_writes_the_user_config_and_keeps_other_keys() -> None:
    path = user_config_document.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "lm:\n  provider: claude_code\n  model: sonnet\n"
        "providers:\n  servers:\n  - id: lm_studio\n    preset_id: lm_studio\n",
        encoding="utf-8",
    )

    outcome = support_record.record_support("claude_code")

    assert outcome.persisted and outcome.reason == ""
    document = _config()
    assert document["lm"] == {"provider": "claude_code", "model": "sonnet"}
    assert document["providers"]["servers"][0]["id"] == "lm_studio"
    assert document["providers"]["installed_support"] == {"claude_code": {}}
    assert support_record.read_recorded_support().entries == {"claude_code": {}}


def test_component_floors_only_move_up_and_rerecording_does_not_rewrite() -> None:
    support_record.record_support("codex", {"openai-codex-cli-bin": "0.158.0"})
    support_record.record_support("codex", {"openai-codex-cli-bin": "0.157.1"})  # older: ignored
    assert support_record.read_recorded_support().entries == {
        "codex": {"openai-codex-cli-bin": "0.158.0"}
    }

    support_record.record_support("codex", {"openai-codex-cli-bin": "0.160.2"})
    path = user_config_document.user_config_path()
    before = path.stat().st_mtime_ns
    content = path.read_text(encoding="utf-8")
    support_record.record_support("codex", {"openai-codex-cli-bin": "0.160.2"})
    support_record.record_support("codex")

    assert path.read_text(encoding="utf-8") == content
    assert path.stat().st_mtime_ns == before
    assert _config()["providers"]["installed_support"] == {
        "codex": {"versions": {"openai-codex-cli-bin": "0.160.2"}}
    }


def test_a_hand_written_list_of_kinds_is_read_as_the_record() -> None:
    path = user_config_document.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("providers:\n  installed_support: [claude_code, argonne]\n", encoding="utf-8")

    assert support_record.read_recorded_support().entries == {"claude_code": {}, "argonne": {}}
    support_record.record_support("claude_code", {"claude-agent-sdk": "0.2.159"})
    assert support_record.read_recorded_support().entries == {
        "argonne": {},
        "claude_code": {"claude-agent-sdk": "0.2.159"},
    }


def test_a_record_that_cannot_be_written_is_a_typed_reported_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("a file where the config directory should be", encoding="utf-8")
    monkeypatch.setattr(user_config_document, "user_config_path", lambda: blocked / "config.yaml")

    outcome = support_record.record_support("claude_code")

    assert not outcome.persisted
    assert outcome.reason == support_record.PROVIDER_SUPPORT_NOT_RECORDED
    assert outcome.detail
    assert [f.provider_kind for f in support_record.last_record_failure()] == ["claude_code"]

    monkeypatch.undo()
    assert support_record.record_support("claude_code").persisted
    assert support_record.last_record_failure() == []


def test_a_malformed_config_is_an_unreadable_record_not_an_empty_one() -> None:
    path = user_config_document.user_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("- just\n- a list\n", encoding="utf-8")

    read = support_record.read_recorded_support()
    assert read.entries == {}
    assert read.reason == support_record.PROVIDER_SUPPORT_RECORD_UNREADABLE

    outcome = support_record.record_support("claude_code")
    assert outcome.reason == support_record.PROVIDER_SUPPORT_NOT_RECORDED
    assert path.read_text(encoding="utf-8") == "- just\n- a list\n"  # never clobbered


def test_concurrent_config_writers_never_drop_each_others_keys() -> None:
    """The LM selection store and the support record rewrite the SAME file.

    With one shared lock neither can read the old document between the other's
    read and write, so both changes survive.
    """
    from clio_agent.gact.local_server_store import add_server

    errors: list[BaseException] = []

    def _record(i: int) -> None:
        try:
            support_record.record_support(f"kind_{i}")
        except BaseException as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    def _server(i: int) -> None:
        try:
            add_server(address=f"http://10.0.0.{i}:8000/v1", label=f"server {i}")
        except BaseException as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=_record, args=(i,)) for i in range(12)]
    threads += [threading.Thread(target=_server, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    document = _config()
    assert len(document["providers"]["installed_support"]) == 12
    assert len(document["providers"]["servers"]) == 12
