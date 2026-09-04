"""Focused normalized-catalog and resource-delivery planner tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from clio_agent.gact.provider_catalog import discover_provider, model_catalog_row
from clio_agent.gact.resource_custody import ResourceRecord
from clio_agent.gact.resource_delivery import (
    ResourceDeliveryRecord,
    ResourceDeliveryStore,
    plan_resource_delivery,
)
from clio_agent.gact.types import LMProviderPreset, ModelRef
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    HandshakeReport,
    ModelProfile,
)


def _preset() -> LMProviderPreset:
    return LMProviderPreset(
        id="local-lab",
        label="Local Lab",
        provider="openai",
        api_base="http://127.0.0.1:9000/v1",
        suggested_model="vision-local",
    )


def _report(*, source: str = "live") -> HandshakeReport:
    return HandshakeReport(
        provider_id="local-lab",
        provider_kind="openai",
        connectivity=ConnectivityState.OK,
        auth=AuthState.NOT_REQUIRED,
        models_source=source,
        generated_at="2026-08-31T12:00:00+00:00",
        models=(
            ModelProfile(
                id="vision-local",
                capabilities=("vision", "pdf_input"),
                context_window=131_072,
                output_limit=8_192,
                is_reasoning=True,
                reasoning_param="reasoning_effort",
                native_tool_calling=True,
            ),
        ),
    )


def _resource(*, media_type: str) -> ResourceRecord:
    return ResourceRecord(
        id="res_test",
        workspace_id="ws_test",
        name="sample.bin",
        claimed_mime=media_type,
        detected_mime=media_type,
        declared_size=10,
        received_size=10,
        sha256="a" * 64,
        state="ready",
    )


def test_catalog_uses_modalities_only_from_current_live_evidence() -> None:
    live = model_catalog_row(_preset(), _report(), _report().models[0])
    assert live["availability"] == "available"
    assert live["modalities"] == ["image", "pdf", "text"]
    assert live["evidence"]["live"] is True
    assert live["reasoning"]["parameter"] == "reasoning_effort"

    static_report = _report(source="static")
    static = model_catalog_row(_preset(), static_report, static_report.models[0])
    assert static["availability"] == "candidate"
    assert static["modalities"] == ["text"]
    assert static["evidence"]["live"] is False


def test_live_codex_sdk_catalog_advertises_its_typed_image_input() -> None:
    preset = LMProviderPreset(
        id="codex",
        label="Codex",
        provider="codex",
        api_base="codex://sdk",
        suggested_model="gpt-5.6-luna",
    )
    report = HandshakeReport(
        provider_id="codex",
        provider_kind="codex",
        connectivity=ConnectivityState.OK,
        auth=AuthState.NOT_REQUIRED,
        models_source="live",
        generated_at="2026-09-02T12:00:00+00:00",
        models=(ModelProfile(id="gpt-5.6-luna", capabilities=("text", "image")),),
    )

    row = model_catalog_row(preset, report, report.models[0])

    assert row["modalities"] == ["image", "text"]
    assert row["evidence"]["live"] is True


def test_normalized_codex_catalog_bootstraps_live_discovery(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    preset = LMProviderPreset(
        id="codex",
        label="Codex",
        provider="codex",
        api_base="codex://app-server",
        suggested_model="",
    )
    overlay_ready = False
    refreshed: list[str] = []

    def _overlay(*_args: object) -> dict[str, object] | None:
        return {"models": [{"id": "gpt-5.6-luna"}]} if overlay_ready else None

    async def _refresh(*, presets, only_configured):  # type: ignore[no-untyped-def]
        nonlocal overlay_ready
        assert only_configured is False
        refreshed.extend(provider.id for provider in presets)
        overlay_ready = True
        return [{"provider": "codex", "failed_reason": ""}]

    async def _handshake(*_args: object, **_kwargs: object) -> HandshakeReport:
        return HandshakeReport(
            provider_id="codex",
            provider_kind="codex",
            connectivity=ConnectivityState.OK,
            auth=AuthState.NOT_REQUIRED,
            models_source="live",
            models=(ModelProfile(id="gpt-5.6-luna"),),
        )

    monkeypatch.setattr(
        "clio_agent.gact.provider_catalog.model_discovery.overlay_models_wire", _overlay
    )
    monkeypatch.setattr("clio_agent.gact.provider_catalog.model_discovery.refresh_all", _refresh)
    monkeypatch.setattr("clio_agent.gact.provider_catalog.run_handshake", _handshake)

    provider = asyncio.run(discover_provider(preset))
    assert refreshed == ["codex"]
    assert [model["model_id"] for model in provider["models"]] == ["gpt-5.6-luna"]
    assert provider["health"] == "ready"


def test_normalized_codex_catalog_hides_static_candidates_after_discovery_failure(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    preset = LMProviderPreset(
        id="codex",
        label="Codex",
        provider="codex",
        api_base="codex://app-server",
        suggested_model="",
    )

    monkeypatch.setattr(
        "clio_agent.gact.provider_catalog.model_discovery.overlay_models_wire",
        lambda *_args: None,
    )

    async def _refresh(*, presets, only_configured):  # type: ignore[no-untyped-def]
        del presets, only_configured
        return [{"provider": "codex", "failed_reason": "app-server unavailable"}]

    async def _handshake(*_args: object, **_kwargs: object) -> HandshakeReport:
        return HandshakeReport(
            provider_id="codex",
            provider_kind="codex",
            connectivity=ConnectivityState.OK,
            auth=AuthState.NOT_REQUIRED,
            models_source="static",
            models=(ModelProfile(id="gpt-5.5"),),
        )

    monkeypatch.setattr("clio_agent.gact.provider_catalog.model_discovery.refresh_all", _refresh)
    monkeypatch.setattr("clio_agent.gact.provider_catalog.run_handshake", _handshake)

    provider = asyncio.run(discover_provider(preset))
    assert provider["models"] == []
    assert provider["health"] == "unavailable"
    assert provider["failure"] == "app-server unavailable"


def test_planner_never_uses_unverified_native_or_changes_provider() -> None:
    app = SimpleNamespace(state=SimpleNamespace(lm_handshake_report=_report()))
    model = ModelRef(provider_id="local-lab", model_id="vision-local")
    native = plan_resource_delivery(
        app,
        resource=_resource(media_type="image/png"),
        message_id="msg_native",
        model=model,
    )
    assert native.representation == "native"
    assert native.provider_id == "local-lab"
    assert native.evidence_source == "live_handshake"

    app.state.lm_handshake_report = _report(source="static")
    unknown = plan_resource_delivery(
        app,
        resource=_resource(media_type="image/png"),
        message_id="msg_unknown",
        model=model,
    )
    assert unknown.representation == "metadata_only"
    assert unknown.provider_id == "local-lab"
    assert unknown.evidence_source == "unavailable"


def test_planner_uses_current_in_process_catalog_without_active_handshake() -> None:
    """The React catalog read primes native delivery for the selected model."""

    app = SimpleNamespace(
        state=SimpleNamespace(
            provider_catalog={
                "providers": [
                    {
                        "id": "codex",
                        "health": "ready",
                        "models": [
                            {
                                "model_id": "gpt-5.6-luna",
                                "availability": "available",
                                "modalities": ["image", "text"],
                                "evidence": {
                                    "live": True,
                                    "generated_at": "2026-09-01T12:00:00+00:00",
                                },
                            }
                        ],
                    }
                ]
            }
        )
    )
    planned = plan_resource_delivery(
        app,
        resource=_resource(media_type="image/png"),
        message_id="msg_catalog_native",
        model=ModelRef(provider_id="codex", model_id="gpt-5.6-luna"),
    )

    assert planned.representation == "native"
    assert planned.evidence_source == "live_handshake"
    assert planned.evidence_generated_at == "2026-09-01T12:00:00+00:00"


def test_delivery_ledger_is_idempotent_and_cascades_workspace_delete(tmp_path: Path) -> None:
    store = ResourceDeliveryStore(tmp_path / "deliveries.json")
    first = ResourceDeliveryRecord(
        workspace_id="ws_test",
        resource_id="res_test",
        resource_revision=1,
        resource_sha256="a" * 64,
        message_id="msg_test",
        provider_id="local-lab",
        model_id="vision-local",
        representation="native",
        evidence_source="live_handshake",
        reason="verified image input",
    )
    saved = store.append(first)
    replay = store.append(first.model_copy(update={"reason": "must not replace"}))
    assert replay.id == saved.id
    assert replay.reason == "verified image input"
    assert len(store.list("ws_test")) == 1

    reloaded = ResourceDeliveryStore(tmp_path / "deliveries.json")
    assert reloaded.delete_workspace("ws_test") == 1
    assert reloaded.list("ws_test") == []


def test_overlay_evidence_is_available_and_dates_itself_to_the_probe() -> None:
    """A persisted discovery run is EVIDENCE, and carries its own timestamp.

    The CLI providers have no HTTP models surface, so their whole catalog arrives
    through the overlay. Treating only ``live`` as evidence would blank them; and
    reporting the handshake run's wall clock over that cached evidence made a
    months-old catalog look freshly generated.
    """

    preset = LMProviderPreset(
        id="codex",
        label="Codex",
        provider="codex",
        api_base="codex://sdk",
        suggested_model="gpt-5.6-luna",
    )
    report = HandshakeReport(
        provider_id="codex",
        provider_kind="codex",
        connectivity=ConnectivityState.OK,
        auth=AuthState.NOT_REQUIRED,
        models_source="overlay",
        generated_at="2026-09-03T09:00:00+00:00",
        evidence_generated_at="2026-01-02T03:04:05+00:00",
        models=(
            ModelProfile(
                id="gpt-5.6-luna",
                capabilities=("text", "image"),
                evidence_generated_at="2026-01-02T03:04:05+00:00",
            ),
        ),
    )

    row = model_catalog_row(preset, report, report.models[0])

    assert row["availability"] == "available"
    assert row["modalities"] == ["image", "text"]
    assert row["evidence"]["evidenced"] is True
    # ``live`` means what it says: no probe ran on this handshake.
    assert row["evidence"]["live"] is False
    assert row["evidence"]["generated_at"] == "2026-01-02T03:04:05+00:00"
    assert row["evidence"]["read_at"] == "2026-09-03T09:00:00+00:00"


def test_static_catalog_rows_are_never_evidence() -> None:
    preset = LMProviderPreset(
        id="codex",
        label="Codex",
        provider="codex",
        api_base="codex://sdk",
        suggested_model="gpt-5.5",
    )
    report = HandshakeReport(
        provider_id="codex",
        provider_kind="codex",
        connectivity=ConnectivityState.OK,
        auth=AuthState.NOT_REQUIRED,
        models_source="static",
        generated_at="2026-09-03T09:00:00+00:00",
        models=(ModelProfile(id="gpt-5.5", capabilities=("text", "image")),),
    )

    row = model_catalog_row(preset, report, report.models[0])

    assert row["availability"] == "candidate"
    assert row["modalities"] == ["text"]
    assert row["evidence"]["evidenced"] is False
    assert row["evidence"]["live"] is False


def test_planner_accepts_overlay_evidence_and_keeps_its_probe_timestamp() -> None:
    """Delivery planning must accept persisted discovery evidence, dated honestly."""

    app = SimpleNamespace(
        state=SimpleNamespace(
            provider_catalog={
                "providers": [
                    {
                        "id": "codex",
                        "health": "ready",
                        "models": [
                            {
                                "model_id": "gpt-5.6-luna",
                                "availability": "available",
                                "modalities": ["text", "image"],
                                "evidence": {
                                    "evidenced": True,
                                    "live": False,
                                    "source": "overlay",
                                    "generated_at": "2026-01-02T03:04:05+00:00",
                                },
                            }
                        ],
                    }
                ]
            }
        )
    )

    planned = plan_resource_delivery(
        app,
        resource=_resource(media_type="image/png"),
        message_id="m_overlay",
        model=ModelRef(provider_id="codex", model_id="gpt-5.6-luna"),
    )

    assert planned.representation == "native"
    assert planned.evidence_source == "discovery_overlay"
    assert planned.evidence_generated_at == "2026-01-02T03:04:05+00:00"


def test_a_stale_overlay_triggers_rediscovery_instead_of_being_served_forever(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    """A written overlay used to short-circuit the bootstrap for good.

    Any non-empty ``models`` list satisfied the guard, so once an entry existed
    nothing ever re-ran discovery and a rotated account catalog was served
    indefinitely. The bootstrap now re-runs when the entry is typed-stale.
    """

    preset = LMProviderPreset(
        id="codex",
        label="Codex",
        provider="codex",
        api_base="codex://sdk",
        suggested_model="",
    )
    refreshed: list[str] = []
    stale = True

    def _overlay(*_args: object) -> dict[str, object] | None:
        entry: dict[str, object] = {"models": [{"id": "gpt-5.6-luna"}]}
        if stale:
            entry["staleness"] = {
                "reason": "overlay_age_exceeded_ttl",
                "description": "older than the configured freshness window",
            }
        return entry

    async def _refresh(*, presets, only_configured):  # type: ignore[no-untyped-def]
        nonlocal stale
        del only_configured
        refreshed.extend(provider.id for provider in presets)
        stale = False
        return [{"provider": "codex", "failed_reason": ""}]

    async def _handshake(*_args: object, **_kwargs: object) -> HandshakeReport:
        return HandshakeReport(
            provider_id="codex",
            provider_kind="codex",
            connectivity=ConnectivityState.OK,
            auth=AuthState.NOT_REQUIRED,
            models_source="overlay",
            models=(ModelProfile(id="gpt-5.6-luna"),),
        )

    monkeypatch.setattr(
        "clio_agent.gact.provider_catalog.model_discovery.overlay_models_wire", _overlay
    )
    monkeypatch.setattr("clio_agent.gact.provider_catalog.model_discovery.refresh_all", _refresh)
    monkeypatch.setattr("clio_agent.gact.provider_catalog.run_handshake", _handshake)

    provider = asyncio.run(discover_provider(preset))

    assert refreshed == ["codex"]
    assert provider["health"] == "ready"
    assert [model["model_id"] for model in provider["models"]] == ["gpt-5.6-luna"]


def test_a_failed_rediscovery_over_prior_evidence_stays_available_but_marked_stale(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    """Prior evidence is not an availability failure -- but it is not fresh either."""

    preset = LMProviderPreset(
        id="codex",
        label="Codex",
        provider="codex",
        api_base="codex://sdk",
        suggested_model="",
    )
    staleness = {
        "reason": "overlay_refresh_failed",
        "description": "the most recent refresh failed",
        "failed_reason": "SDK transport closed",
    }

    monkeypatch.setattr(
        "clio_agent.gact.provider_catalog.model_discovery.overlay_models_wire",
        lambda *_args: {"models": [{"id": "gpt-5.6-luna"}], "staleness": staleness},
    )

    async def _refresh(*, presets, only_configured):  # type: ignore[no-untyped-def]
        del presets, only_configured
        return [{"provider": "codex", "failed_reason": "SDK transport closed"}]

    async def _handshake(*_args: object, **_kwargs: object) -> HandshakeReport:
        return HandshakeReport(
            provider_id="codex",
            provider_kind="codex",
            connectivity=ConnectivityState.OK,
            auth=AuthState.NOT_REQUIRED,
            models_source="overlay",
            models=(ModelProfile(id="gpt-5.6-luna"),),
        )

    monkeypatch.setattr("clio_agent.gact.provider_catalog.model_discovery.refresh_all", _refresh)
    monkeypatch.setattr("clio_agent.gact.provider_catalog.run_handshake", _handshake)

    provider = asyncio.run(discover_provider(preset))

    assert provider["health"] == "ready"
    assert [model["model_id"] for model in provider["models"]] == ["gpt-5.6-luna"]
    assert provider["freshness"]["staleness"]["reason"] == "overlay_refresh_failed"
