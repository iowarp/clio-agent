"""A bind-time key must not outlive a managed deployment's key rotation (F021)."""

from __future__ import annotations

import pytest

from clio_agent.providers import api_key_store
from clio_agent.providers.credentials import boot_credential_stays_current
from clio_agent.providers.lm_spec import LMSpec
from clio_agent.providers.resolver import resolve_endpoint_and_handshake


def vllm_spec() -> LMSpec:
    return LMSpec(provider="vllm", model="m", provider_id="vllm", api_base="http://127.0.0.1:9/v1")


def test_a_durably_saved_key_makes_the_boot_key_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = {"vllm": "rotated"}
    monkeypatch.setattr(api_key_store, "stored_api_key", lambda provider: keys.get(provider, ""))
    assert not boot_credential_stays_current(vllm_spec())
    keys.clear()
    assert boot_credential_stays_current(vllm_spec())


def test_argonne_never_reuses_a_captured_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api_key_store, "stored_api_key", lambda provider: "")
    assert not boot_credential_stays_current(LMSpec(provider="argonne", model="m"))


class FreshResolver:
    def resolve(self, provider: str, ref: str) -> str:
        return "rotated"


def test_without_a_boot_key_the_turn_uses_the_freshly_resolved_key() -> None:
    resolved = resolve_endpoint_and_handshake(vllm_spec(), default_credential="")
    assert resolved.materialize(FreshResolver()).api_key == "rotated"  # type: ignore[arg-type]
    stale = resolve_endpoint_and_handshake(vllm_spec(), default_credential="bind-time")
    assert stale.materialize(FreshResolver()).api_key == "bind-time"  # type: ignore[arg-type]
