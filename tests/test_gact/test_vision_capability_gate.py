"""The image-input gate resolves capability from EVIDENCE, not a provider name.

``_active_lm_supports_vision`` used to end in a literal ``{"openai",
"anthropic"}`` name allowlist, and the ``supports_vision`` key it preferred was
never written by any production path — so the catalog's own
``supports_vision=True`` flags (codex, claude_code) could not reach the gate at
all, and no amount of discovery evidence could either. These tests drive each
typed arm of the replacement through the real resolver.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clio_agent.gact.providers.config import (
    VISION_CAPABILITY_REASONS,
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


def _catalog(provider_id: str, model_id: str, modalities: list[str]) -> dict[str, Any]:
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
    assert set(VISION_CAPABILITY_REASONS) == {
        "live_modality_evidence",
        "catalog_default_no_modality_evidence_system",
        "modality_evidence_unavailable",
        "no_active_model",
    }
    assert all(sentence for sentence in VISION_CAPABILITY_REASONS.values())


def test_discovery_evidence_naming_image_permits_image_parts() -> None:
    app = _app(catalog=_catalog("claude_code", "sonnet", ["text", "image", "pdf"]))
    assert _vision_capability(app, "claude_code", "sonnet") == (True, "live_modality_evidence")


def test_discovery_evidence_omitting_image_refuses_even_for_a_vision_flagged_catalog() -> None:
    """Evidence outranks the catalog default -- a text-only model stays text-only.

    ``claude_code`` carries ``supports_vision=True`` in the static registry; the
    gate must not let that flag override what discovery actually reported.
    """

    app = _app(catalog=_catalog("claude_code", "haiku", ["text"]))
    assert _vision_capability(app, "claude_code", "haiku") == (False, "live_modality_evidence")


def test_a_provider_with_an_evidence_system_but_no_evidence_yet_is_refused() -> None:
    """ "Not evidenced yet" is a different, actionable answer from "cannot be asked"."""

    app = _app()
    assert _vision_capability(app, "codex", "gpt-5.5") == (
        False,
        "modality_evidence_unavailable",
    )


def test_a_provider_with_no_evidence_system_uses_the_documented_catalog_default() -> None:
    """An OpenAI-compatible /models listing returns ids and nothing else."""

    app = _app()
    assert _vision_capability(app, "openai", "gpt-4o") == (
        True,
        "catalog_default_no_modality_evidence_system",
    )
    assert _vision_capability(app, "anthropic", "claude-sonnet-4-20250514") == (
        True,
        "catalog_default_no_modality_evidence_system",
    )
    assert _vision_capability(app, "vllm", "Qwen/Qwen2.5-VL-7B-Instruct") == (
        True,
        "catalog_default_no_modality_evidence_system",
    )
    # ...and a catalog-level flag of False is honoured just as literally.
    assert _vision_capability(app, "lm_studio", "qwen")[0] is False


def test_no_bound_model_is_its_own_typed_arm() -> None:
    assert _vision_capability(_app(), "", "") == (False, "no_active_model")


def test_effective_config_forwards_the_field_the_gate_reads() -> None:
    """The gate reads ``supports_vision``; the config must actually carry it.

    Nothing wrote that key before, on any path -- including the unconfigured
    early return -- so the gate could only ever fall through to the name
    allowlist.
    """

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


def test_a_hand_set_config_value_cannot_fabricate_the_capability() -> None:
    """The key is DERIVED, never trusted from the config dict.

    A caller-set ``supports_vision`` was the only way the old gate could be
    satisfied without a name match -- and no production writer ever set it, so it
    was an unreachable value that only tests could reach.
    """

    app = _app(lm_config={"provider": "codex", "model": "gpt-5.5", "supports_vision": True})
    assert _active_lm_supports_vision(app) is False
    assert _effective_lm_config(app)["supports_vision_source"] == "modality_evidence_unavailable"
