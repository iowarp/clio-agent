"""The image-input gate resolves capability from EVIDENCE, three-valued.

``_active_lm_supports_vision`` used to end in a literal ``{"openai",
"anthropic"}`` name allowlist; later it read a catalog row's ``modalities``
as proof even when discovery had established none, so every ALCF model (whose
gateway ``/models`` rows carry no modality fields) was refused images --
Llama-3.2-90B-Vision included. The gate now answers from
:func:`clio_agent.gact.modality_evidence.image_input_capability`: known
modalities decide, and UNKNOWN is permitted under the typed
``modality_unknown`` reason rather than refused. These tests drive each typed
arm through the real resolver.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clio_agent.gact.modality_evidence import IMAGE_INPUT_REASONS
from clio_agent.gact.providers.config import (
    _active_lm_supports_vision,
    _effective_lm_config,
    _vision_capability,
)


def _app(*, lm_config: dict[str, Any] | None = None, catalog: Any = None) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            lm_config=lm_config or {},
            agent=None,
            provider_catalog=catalog,
            lm_handshake_report=None,
        )
    )


def _catalog(
    provider_id: str,
    model_id: str,
    modalities: list[str],
    *,
    modality_evidenced: bool = True,
) -> dict[str, Any]:
    return {
        "providers": [
            {
                "id": provider_id,
                "health": "ready",
                "models": [
                    {
                        "model_id": model_id,
                        "availability": "available",
                        "modalities": modalities,
                        "evidence": {
                            "evidenced": True,
                            "modality_evidenced": modality_evidenced,
                            "live": True,
                            "source": "live",
                            "generated_at": "2026-09-03T00:00:00+00:00",
                        },
                    }
                ],
            }
        ]
    }


def test_every_reason_arm_is_catalogued() -> None:
    assert set(IMAGE_INPUT_REASONS) == {
        "live_modality_evidence",
        "modality_unknown",
        "no_active_model",
    }
    assert all(sentence for sentence in IMAGE_INPUT_REASONS.values())


def test_discovery_evidence_naming_image_permits_image_parts() -> None:
    app = _app(catalog=_catalog("claude_code", "sonnet", ["text", "image", "pdf"]))
    assert _vision_capability(app, "claude_code", "sonnet") == (True, "live_modality_evidence")


def test_known_modalities_omitting_image_refuse() -> None:
    """A KNOWN text-only model stays text-only, whatever the provider."""

    app = _app(catalog=_catalog("claude_code", "haiku", ["text"]))
    assert _vision_capability(app, "claude_code", "haiku") == (False, "live_modality_evidence")


def test_an_evidenced_row_with_unknown_modalities_is_unknown_not_text_only() -> None:
    """The ALCF case: the model is live-evidenced, its modalities are not.

    Before the fix the row's placeholder ``["text"]`` was read as proof and the
    gate refused; unknown must be permitted under its own typed reason.
    """

    app = _app(
        catalog=_catalog(
            "argonne_sophia",
            "meta-llama/Llama-3.2-90B-Vision-Instruct",
            [],
            modality_evidenced=False,
        )
    )
    assert _vision_capability(
        app, "argonne_sophia", "meta-llama/Llama-3.2-90B-Vision-Instruct"
    ) == (True, "modality_unknown")


def test_no_evidence_at_all_is_unknown_and_permitted() -> None:
    """No catalog row yet is also UNKNOWN: absence of evidence refuses nothing."""

    app = _app()
    for provider_id, model_id in (
        ("codex", "gpt-5.5"),
        ("openai", "gpt-4o"),
        ("lm_studio", "qwen"),
        ("vllm", "Qwen/Qwen2.5-VL-7B-Instruct"),
    ):
        assert _vision_capability(app, provider_id, model_id) == (True, "modality_unknown")


def test_no_bound_model_is_its_own_typed_arm() -> None:
    assert _vision_capability(_app(), "", "") == (False, "no_active_model")


def test_effective_config_forwards_the_field_the_gate_reads() -> None:
    """The gate reads ``supports_vision``; the config must actually carry it."""

    app = _app(
        lm_config={"provider": "claude_code", "model": "sonnet"},
        catalog=_catalog("claude_code", "sonnet", ["text", "image"]),
    )
    cfg = _effective_lm_config(app)
    assert cfg["supports_vision"] is True
    assert cfg["supports_vision_source"] == "live_modality_evidence"
    assert _active_lm_supports_vision(app) is True

    unconfigured = _app()
    assert _effective_lm_config(unconfigured)["supports_vision"] is False
    assert _effective_lm_config(unconfigured)["supports_vision_source"] == "no_active_model"


def test_a_hand_set_config_value_cannot_override_the_evidence() -> None:
    """The key is DERIVED, never trusted from the config dict -- in either direction."""

    app = _app(
        lm_config={"provider": "claude_code", "model": "haiku", "supports_vision": True},
        catalog=_catalog("claude_code", "haiku", ["text"]),
    )
    assert _active_lm_supports_vision(app) is False
    assert _effective_lm_config(app)["supports_vision_source"] == "live_modality_evidence"
