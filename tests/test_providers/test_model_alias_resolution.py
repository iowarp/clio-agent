"""A configured model id may be a catalog/handshake ALIAS, not the canonical id.

``{provider_id: "claude_code", model: "sonnet"}`` is a real, supported binding --
``PUT /v1/providers/lm`` itself reports ``resolved_model_id: "claude-sonnet-5"``
for it -- but before this fix every modality/capability lookup compared the
configured value against catalog/handshake model identity with a bare ``==``,
so an alias-bound selection was silently treated as an unknown model: no
``view_image``/``view_pdf``, no native image/PDF delivery.

:func:`~clio_agent.providers.handshake.model.resolve_model_id` is the ONE
resolution point every one of those lookups now routes through --
``HandshakeReport.model`` directly, and ``gact.resource_delivery.
_catalog_modalities`` explicitly. These tests drive an alias binding through
each evidence shape (a bare ``HandshakeReport``, an in-process provider
catalog dict) and through the capability gates / declared-tool resolution that
sit on top of them, and pin that an UNRELATED id is never mistaken for a real
alias.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact import context as gact_context
from clio_agent.gact.agents.declared_native_tools import (
    declared_native_capabilities,
    resolve_declared_native_tools,
)
from clio_agent.gact.catalog import _builtin_main_agent
from clio_agent.gact.modality_evidence import live_model_modalities
from clio_agent.gact.providers.config import _pdf_capability, _vision_capability
from clio_agent.gact.types import ModelRef
from clio_agent.providers.capabilities import invalidation
from clio_agent.providers.capabilities.records import (
    DeploymentCapabilities,
    Fact,
    ModelCapabilities,
)
from clio_agent.providers.handshake.model import (
    AuthState,
    ConnectivityState,
    DiscoveredModel,
    HandshakeReport,
    resolve_model_id,
)

_CANONICAL = "claude-sonnet-5"
_ALIAS = "sonnet"
_UNRELATED = "sonnet-x"
_NOW = "2026-09-24T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _clear_capability_store():
    invalidation.clear_all()
    yield
    invalidation.clear_all()


def _handshake_report() -> HandshakeReport:
    """A report + the capability-store facts ``live_model_modalities`` needs.

    ``HandshakeReport`` itself only carries bare identity (:class:`DiscoveredModel`);
    the modalities a caller resolves through it now come from the accessor, so
    this seeds the SAME store a real handshake would have written to.
    """
    invalidation.record_model_capabilities(
        ModelCapabilities(
            model_key=_CANONICAL,
            input_modalities=Fact(
                value=frozenset({"image", "pdf", "text"}), source="server_report", observed_at=_NOW
            ),
        )
    )
    invalidation.record_deployment_capabilities(
        DeploymentCapabilities(
            provider_id="claude_code",
            api_base="",
            model_id=_CANONICAL,
            model_key=Fact(value=_CANONICAL, source="server_report", observed_at=_NOW),
        )
    )
    return HandshakeReport(
        provider_id="claude_code",
        provider_kind="claude_code",
        connectivity=ConnectivityState.OK,
        auth=AuthState.OK,
        api_base="",
        models_source="overlay",
        generated_at="2026-09-24T00:00:00+00:00",
        models=(
            DiscoveredModel(
                id=_CANONICAL,
                aliases=(_ALIAS,),
                raw={"cli_values": [_ALIAS], "capabilities": ["image", "pdf", "text"]},
            ),
        ),
    )


def _catalog_payload() -> dict[str, Any]:
    return {
        "providers": [
            {
                "id": "claude_code",
                "health": "ready",
                "models": [
                    {
                        "model_id": _CANONICAL,
                        "availability": "available",
                        "modalities": ["image", "pdf", "text"],
                        "aliases": [_ALIAS],
                        "evidence": {
                            "evidenced": True,
                            "live": False,
                            "source": "overlay",
                            "generated_at": "2026-09-24T00:00:00+00:00",
                        },
                    }
                ],
            }
        ]
    }


def _app(*, catalog: Any = None, report: Any = None) -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            lm_config={},
            agent=None,
            provider_catalog=catalog,
            lm_handshake_report=report,
        )
    )


# ---------------------------------------------------------------------------
# resolve_model_id -- the shared resolution point itself
# ---------------------------------------------------------------------------


def test_resolve_model_id_matches_exact_id() -> None:
    assert resolve_model_id([(_CANONICAL, (_ALIAS,))], _CANONICAL) == _CANONICAL


def test_resolve_model_id_matches_an_exact_alias() -> None:
    assert resolve_model_id([(_CANONICAL, (_ALIAS,))], _ALIAS) == _CANONICAL


def test_resolve_model_id_never_keyword_matches_an_unrelated_id() -> None:
    """``sonnet-x`` is not ``sonnet`` -- an exact alias match only."""

    assert resolve_model_id([(_CANONICAL, (_ALIAS,))], _UNRELATED) == _UNRELATED


def test_resolve_model_id_empty_query_is_unchanged() -> None:
    assert resolve_model_id([(_CANONICAL, (_ALIAS,))], "") == ""


# ---------------------------------------------------------------------------
# HandshakeReport.model() -- the report-level caller
# ---------------------------------------------------------------------------


def test_handshake_report_model_resolves_a_cli_alias() -> None:
    report = _handshake_report()
    profile = report.model(_ALIAS)
    assert profile is not None
    assert profile.id == _CANONICAL


def test_handshake_report_model_does_not_match_an_unrelated_id() -> None:
    assert _handshake_report().model(_UNRELATED) is None


# ---------------------------------------------------------------------------
# live_model_modalities / _catalog_modalities -- the in-process catalog caller
# ---------------------------------------------------------------------------


def test_live_model_modalities_resolves_the_alias_against_the_catalog() -> None:
    app = _app(catalog=_catalog_payload())
    found = live_model_modalities(app, ModelRef(provider_id="claude_code", model_id=_ALIAS))
    assert "image" in (found.modalities or ())
    assert "pdf" in (found.modalities or ())
    assert found.evidence == "discovery_overlay"


def test_live_model_modalities_resolves_the_alias_against_a_handshake_report() -> None:
    app = _app(report=_handshake_report())
    found = live_model_modalities(app, ModelRef(provider_id="claude_code", model_id=_ALIAS))
    assert "image" in (found.modalities or ())
    assert found.evidence == "discovery_overlay"


def test_live_model_modalities_does_not_resolve_an_unrelated_id() -> None:
    app = _app(catalog=_catalog_payload())
    found = live_model_modalities(app, ModelRef(provider_id="claude_code", model_id=_UNRELATED))
    assert found.modalities is None
    assert found.evidence == "unavailable"


# ---------------------------------------------------------------------------
# _vision_capability / _pdf_capability -- the gate callers
# ---------------------------------------------------------------------------


def test_vision_capability_is_true_for_an_alias_bound_model() -> None:
    app = _app(catalog=_catalog_payload())
    assert _vision_capability(app, "claude_code", _ALIAS) == (True, "live_modality_evidence")


def test_pdf_capability_is_true_for_an_alias_bound_model() -> None:
    app = _app(catalog=_catalog_payload())
    assert _pdf_capability(app, "claude_code", _ALIAS) == (True, "live_modality_evidence")


def test_vision_and_pdf_capability_treat_an_unrelated_id_as_unknown() -> None:
    """An id the catalog does not resolve borrows nothing: its capability is UNKNOWN.

    Unknown image input is permitted under ``modality_unknown`` (the upstream
    endpoint decides); unknown PDF input withholds native PDF. Neither answer
    claims the alias's evidence.
    """

    app = _app(catalog=_catalog_payload())
    assert _vision_capability(app, "claude_code", _UNRELATED) == (True, "modality_unknown")
    assert _pdf_capability(app, "claude_code", _UNRELATED) == (False, "modality_unknown")


# ---------------------------------------------------------------------------
# Declared native tools -- the default agent's resolved tool surface
# ---------------------------------------------------------------------------


def test_default_agent_resolves_view_image_and_view_pdf_for_an_alias_bound_model() -> None:
    """An agent bound to {provider_id: claude_code, model: sonnet} keeps its native tools.

    Before the fix, ``declared_native_capabilities`` (via ``_vision_capability``/
    ``_pdf_capability``) answered False for the alias, so ``resolve_declared_native_tools``
    dropped ``view_image``/``view_pdf`` from the default agent's tool surface even
    though it declares both (see ``_builtin_main_agent``).
    """

    app = _app(catalog=_catalog_payload())
    gact_context.set_turn_identity(
        app=app, session_id="sess_test", turn_id="turn_test", trace_id="trace_test"
    )
    try:
        config = SimpleNamespace(provider_id="claude_code", model=_ALIAS)
        capabilities = declared_native_capabilities(config)
        assert capabilities == {"supports_vision": True, "supports_pdf": True}

        agent = _builtin_main_agent()
        assert "view_image" in agent.tools
        assert "view_pdf" in agent.tools
        requested, available, _gateway = resolve_declared_native_tools(agent, {}, **capabilities)
        assert "view_image" in requested
        assert "view_image" in available
        assert "view_pdf" in requested
        assert "view_pdf" in available
    finally:
        gact_context.set_turn_identity(app=None, session_id="", turn_id="", trace_id="")
