"""A pinned model with stale discovery is re-discovered before the message gate decides."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import modality_evidence, provider_catalog_snapshot
from clio_agent.gact import selected_model_refresh as refresh_mod
from clio_agent.gact.modality_evidence import ModalityEvidence
from clio_agent.gact.types import ModelRef

_ACTIVE = {"provider_id": "vllm", "model": "/models/qwen3-1.7b"}


def _app(session_model: ModelRef | None) -> Any:
    session = SimpleNamespace(model=session_model)
    return SimpleNamespace(
        state=SimpleNamespace(
            sessions={"sess_1": session},
            lm_config=dict(_ACTIVE),
            agent=None,
            lm_handshake_report=None,
            provider_catalog=None,
        )
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    evidenced = {"value": False}

    async def _read_catalog(app: Any, *, refresh: bool = False, provider_id: str = "") -> dict:
        seen.append({"refresh": refresh, "provider_id": provider_id})
        evidenced["value"] = True
        return {}

    def _modalities(app: Any, model: ModelRef) -> ModalityEvidence:
        return ModalityEvidence(None, "live_handshake" if evidenced["value"] else "unavailable", "")

    monkeypatch.setattr(provider_catalog_snapshot, "read_catalog", _read_catalog)
    monkeypatch.setattr(modality_evidence, "live_model_modalities", _modalities)
    return seen


@pytest.mark.asyncio
async def test_a_pin_without_evidence_rediscovers_only_its_provider(calls: list) -> None:
    app = _app(ModelRef(provider_id="vllm", model_id="/models/qwen3-4b"))
    req = SimpleNamespace(model=None)

    await refresh_mod.refresh_for_selected_model(app, "sess_1", req)  # type: ignore[arg-type]

    assert calls == [{"refresh": True, "provider_id": "vllm"}]


@pytest.mark.asyncio
async def test_a_pin_matching_the_active_lm_is_not_rediscovered(calls: list) -> None:
    app = _app(ModelRef(provider_id="vllm", model_id=_ACTIVE["model"]))

    await refresh_mod.refresh_for_selected_model(app, "sess_1", SimpleNamespace(model=None))  # type: ignore[arg-type]

    assert calls == []


@pytest.mark.asyncio
async def test_no_pin_is_not_rediscovered(calls: list) -> None:
    await refresh_mod.refresh_for_selected_model(_app(None), "sess_1", SimpleNamespace(model=None))  # type: ignore[arg-type]

    assert calls == []


@pytest.mark.asyncio
async def test_a_failed_refresh_never_raises(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    async def _boom(app: Any, **_: Any) -> dict:
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(provider_catalog_snapshot, "read_catalog", _boom)
    app = _app(ModelRef(provider_id="vllm", model_id="/models/qwen3-4b"))

    await refresh_mod.refresh_for_selected_model(app, "sess_1", SimpleNamespace(model=None))  # type: ignore[arg-type]
