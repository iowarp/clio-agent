"""The PDF-input gate resolves capability from EVIDENCE, and surfaces WHY.

Mirrors ``test_vision_capability_gate.py``. ``declared_view_pdf_capability``
discarded its typed reason, ``_effective_lm_config`` stamped only
``supports_vision``/``supports_vision_source``, and ``PDF_CAPABILITY_REASONS``
had zero consumers -- so a PDF-capability decision (and why it was made)
never reached the effective config or the wire, unlike vision's. A model
could silently lose ``view_pdf`` with no way to see why. These tests drive
each typed arm of ``_pdf_capability`` through the real resolver and lock in
that ``_effective_lm_config`` carries ``supports_pdf``/``supports_pdf_source``
exactly the way it already carries the vision pair.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clio_agent.gact.providers.config import (
    PDF_CAPABILITY_REASONS,
    _effective_lm_config,
    _pdf_capability,
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


def test_every_pdf_reason_arm_is_catalogued() -> None:
    assert set(PDF_CAPABILITY_REASONS) == {
        "live_modality_evidence",
        "modality_unknown",
        "no_active_model",
    }
    assert all(sentence for sentence in PDF_CAPABILITY_REASONS.values())


def test_discovery_evidence_naming_pdf_permits_pdf_parts() -> None:
    app = _app(catalog=_catalog("claude_code", "opus", ["text", "image", "pdf"]))
    assert _pdf_capability(app, "claude_code", "opus") == (True, "live_modality_evidence")


def test_discovery_evidence_omitting_pdf_refuses_even_for_an_image_capable_model() -> None:
    """A model that evidences image but not pdf stays pdf-refused (codex-shaped)."""

    app = _app(catalog=_catalog("codex", "gpt-5.5", ["text", "image"]))
    assert _pdf_capability(app, "codex", "gpt-5.5") == (False, "live_modality_evidence")


def test_unknown_pdf_capability_withholds_native_pdf_under_a_typed_reason() -> None:
    """No row, or a row that states no modalities, is UNKNOWN -- never a known "no".

    Native PDF stays withheld (the structured conversion still carries the
    document), but the reason names the unknown instead of claiming evidence.
    """

    app = _app()
    assert _pdf_capability(app, "codex", "gpt-5.5") == (False, "modality_unknown")
    assert _pdf_capability(app, "openai", "gpt-4o") == (False, "modality_unknown")

    catalog = _catalog("argonne_sophia", "google/gemma-3-27b-it", [])
    catalog["providers"][0]["models"][0]["evidence"]["modality_evidenced"] = False
    assert _pdf_capability(_app(catalog=catalog), "argonne_sophia", "google/gemma-3-27b-it") == (
        False,
        "modality_unknown",
    )


def test_no_bound_model_is_its_own_typed_pdf_arm() -> None:
    assert _pdf_capability(_app(), "", "") == (False, "no_active_model")


def test_effective_config_forwards_the_pdf_field_the_gate_reads() -> None:
    """The declared-tool gate reads ``supports_pdf``; the effective config must
    actually carry it, exactly the way it already carries ``supports_vision``."""

    app = _app(
        lm_config={"provider": "claude_code", "model": "opus"},
        catalog=_catalog("claude_code", "opus", ["text", "image", "pdf"]),
    )
    cfg = _effective_lm_config(app)
    assert cfg["supports_pdf"] is True
    assert cfg["supports_pdf_source"] == "live_modality_evidence"

    unconfigured = _app()
    assert _effective_lm_config(unconfigured)["supports_pdf"] is False
    assert _effective_lm_config(unconfigured)["supports_pdf_source"] == "no_active_model"


def test_a_hand_set_config_value_cannot_fabricate_the_pdf_capability() -> None:
    app = _app(lm_config={"provider": "codex", "model": "gpt-5.5", "supports_pdf": True})
    assert _effective_lm_config(app)["supports_pdf"] is False
    assert _effective_lm_config(app)["supports_pdf_source"] == "modality_unknown"
