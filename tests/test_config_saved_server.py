"""A saved custom model server runs as its preset's protocol at its own address with its own key."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from clio_agent import config
from clio_agent.gact import local_server_store
from clio_agent.providers import model_discovery


def test_a_saved_server_resolves_to_its_preset_address_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = SimpleNamespace(
        id="server-vllm-spotter", preset_id="vllm", address="http://127.0.0.1:35507/v1"
    )
    monkeypatch.setattr(
        local_server_store, "get_server", lambda pid: entry if pid == entry.id else None
    )
    monkeypatch.setattr(
        model_discovery,
        "resolve_cloud_api_key",
        lambda pid: "spotter-key" if pid == entry.id else "",
    )
    cfg = config.LMProviderConfig(provider_id="server-vllm-spotter", model="granite")
    assert cfg.provider_id == "vllm"
    assert cfg.api_base == "http://127.0.0.1:35507/v1"
    assert cfg.api_key == "spotter-key"


def test_an_unknown_provider_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_server_store, "get_server", lambda pid: None)
    with pytest.raises(ValueError, match="Unknown LM provider"):
        config.LMProviderConfig(provider_id="server-missing", model="m")
