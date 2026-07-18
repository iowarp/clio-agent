"""Guard the stable import surface of ``clio_agent.gact.app`` (#714).

The gact decomposition (iowarp/clio-agent#714) carves ``app.py`` -- a ~24k-line
monolith -- into ``runtime/``, ``agents/``, ``emit/``, ``routes/`` and friends.
As code moves out, ``app.py`` must keep working as a thin assembly + re-export
shim so that nothing importing from ``clio_agent.gact.app`` breaks.

This test pins that re-export shim. It asserts that the public API
(``build_app``, ``main``, ``_ACTIVE_GACT_APP``) plus a curated set of private
test-seam symbols remain importable from ``clio_agent.gact.app``. These private
symbols are imported by the existing test suite and by sibling modules; keeping
them importable from the shim is what lets the decomposition proceed in
behaviour-preserving steps. When a symbol legitimately moves, update its new
home but keep a re-export here until the dependents are migrated.
"""

from __future__ import annotations

import importlib

MODULE_NAME = "clio_agent.gact.app"

# Public API that must always be importable from the shim.
PUBLIC_SYMBOLS: list[str] = [
    "build_app",
    "main",
    "_ACTIVE_GACT_APP",
]

# Private test-seam symbols depended on by the test suite / sibling modules.
# Each must stay importable from clio_agent.gact.app as the shim re-exports
# decomposed internals (#714).
SEAM_SYMBOLS: list[str] = [
    "_agent_forward_compat",
    "_agent_streaming_unsupported_reason",
    "_blueprint_runtime_signature",
    "_build_tool_user_agent_module",
    "_estimate_cost_usd",
    "_format_sse",
    "_gact_turn_timeout_s",
    "_make_permission_gate",
    "_make_tool_observer",
    "_not_implemented",
    "_parse_field_annotation",
    "_record_stream_fallback",
    "_refresh_argonne_lm_token",
    "_replace_session_messages",
    "_resolve_runtime_dynamic_agent",
    "_tool_session_context",
]


def _assert_importable(symbol: str) -> None:
    module = importlib.import_module(MODULE_NAME)
    sentinel = object()
    value = getattr(module, symbol, sentinel)
    assert value is not sentinel, (
        f"{MODULE_NAME}.{symbol} is no longer importable -- the gact re-export "
        f"shim regressed (#714). Re-export it from app.py until all dependents "
        f"are migrated to its new home."
    )


def test_public_build_app_importable() -> None:
    _assert_importable("build_app")


def test_public_main_importable() -> None:
    _assert_importable("main")


def test_public_active_gact_app_importable() -> None:
    _assert_importable("_ACTIVE_GACT_APP")


def test_seam_agent_forward_compat_importable() -> None:
    _assert_importable("_agent_forward_compat")


def test_seam_agent_streaming_unsupported_reason_importable() -> None:
    _assert_importable("_agent_streaming_unsupported_reason")


def test_seam_blueprint_runtime_signature_importable() -> None:
    _assert_importable("_blueprint_runtime_signature")


def test_seam_build_tool_user_agent_module_importable() -> None:
    _assert_importable("_build_tool_user_agent_module")


def test_seam_estimate_cost_usd_importable() -> None:
    _assert_importable("_estimate_cost_usd")


def test_seam_format_sse_importable() -> None:
    _assert_importable("_format_sse")


def test_seam_gact_turn_timeout_s_importable() -> None:
    _assert_importable("_gact_turn_timeout_s")


def test_seam_make_permission_gate_importable() -> None:
    _assert_importable("_make_permission_gate")


def test_seam_make_tool_observer_importable() -> None:
    _assert_importable("_make_tool_observer")


def test_seam_not_implemented_importable() -> None:
    _assert_importable("_not_implemented")


def test_seam_parse_field_annotation_importable() -> None:
    _assert_importable("_parse_field_annotation")


def test_seam_record_stream_fallback_importable() -> None:
    _assert_importable("_record_stream_fallback")


def test_seam_refresh_argonne_lm_token_importable() -> None:
    _assert_importable("_refresh_argonne_lm_token")


def test_seam_replace_session_messages_importable() -> None:
    _assert_importable("_replace_session_messages")


def test_seam_resolve_runtime_dynamic_agent_importable() -> None:
    _assert_importable("_resolve_runtime_dynamic_agent")


def test_seam_tool_session_context_importable() -> None:
    _assert_importable("_tool_session_context")
