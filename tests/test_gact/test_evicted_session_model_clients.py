"""An idle session the resident set evicts no longer keeps its model clients.

A multi-agent run creates a child session per member; each finished child kept its LM
and connection pools until the session was deleted (#1846). Eviction now releases them,
and a later turn of the session builds new ones.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.config import LMProviderConfig
from clio_agent.gact import evicted_session
from clio_agent.lm.session_clients import SessionModelClients

CONFIG = LMProviderConfig(provider="lm_studio", model="qwen", api_key="lm-studio")


@pytest.fixture
def owner(monkeypatch: pytest.MonkeyPatch) -> SessionModelClients:
    monkeypatch.setattr("clio_agent.config.create_lm", lambda config: object())
    return SessionModelClients()


def test_an_idle_sessions_clients_close_and_the_session_stays_open(
    owner: SessionModelClients,
) -> None:
    entry, _ = owner.acquire("sess_child", "agent", CONFIG)
    owner.release(entry)

    assert owner.release_idle("sess_child") == [entry.lm]
    again, _ = owner.acquire("sess_child", "agent", CONFIG)  # not refused as deleted
    assert again.lm is not entry.lm


def test_a_client_in_use_closes_on_its_final_release(owner: SessionModelClients) -> None:
    entry, _ = owner.acquire("sess_child", "agent", CONFIG)

    assert owner.release_idle("sess_child") == []
    assert owner.release(entry) == [entry.lm]


def test_eviction_releases_the_sessions_model_clients_unless_it_turned_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released: list[str] = []
    monkeypatch.setattr(evicted_session, "schedule_off_loop", lambda job, label: job())
    monkeypatch.setattr(
        "clio_agent.lm.session_clients.release_idle_session_model_clients",
        lambda app, sid: released.append(sid),
    )
    app: Any = SimpleNamespace(state=SimpleNamespace())

    evicted_session.forget_evicted_session(app, "sess_idle", is_active=lambda: False)
    evicted_session.forget_evicted_session(app, "sess_busy", is_active=lambda: True)

    assert released == ["sess_idle"]
