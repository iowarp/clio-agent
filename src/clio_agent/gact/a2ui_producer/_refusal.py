"""Typed producer-tool refusals — TOOL RESULTS the model can read (S4).

A producer mistake (unknown catalog, failing component schema, no client
catalog selectable, a settled transcript) is returned as
``{"ok": False, "reason": <code>, "detail": ..., "hint": ...}``, never raised
— the model corrects course from the result, the same way it reads any other
tool observation (⚑ #1: clio surfaces reality, it does not decide FOR the
model by throwing).

S8 (issue #1374, live-gate comment): the idle-cell evidence
(``.grind/traces/test_earthscope_a2ui_idle_selection_claude_code_sonnet/
sess_abe5460cf5ae.semantic.jsonl``) showed ``create_a2ui_surface`` refused 14
times in one turn with the SAME reason
(``a2ui_client_capabilities_unknown``) because its ``detail``/``hint`` were
not actionable — a resuming model had nothing to act on but retry. Every
refusal reason now carries a default, reason-specific ``hint`` stating what
is true about the session and what to do INSTEAD of retrying (``detail``
stays the specific, session-scoped fact; ``hint`` stays the actionable next
step, mirroring the existing ``catalog_hint``/``component_hint`` pattern). A
caller-supplied ``hint`` (the mechanical ``load_skill(...)`` calls) always
wins over the default. Separately, the SAME reason recurring within one turn
now records a typed ``a2ui_producer_refusal_repeated`` ledger reason —
observability only, never a cap or a reroute (⚑ #1): this call's return value
never changes because of it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from clio_agent.gact import context as _ctx

if TYPE_CHECKING:
    from clio_agent.gact.a2ui_capability_selection import CatalogSelection

#: Matches the ``component=<Name>`` prefix ``validate_components`` stamps on
#: a schema-failure message (``gact/a2ui_catalogs/validation.py``).
_COMPONENT_EQ_RE = re.compile(r"component=(\S+)")
#: Matches "A2UI component is not in catalog <id>: <Name>" (an unimplemented
#: component name, same module).
_NOT_IN_CATALOG_RE = re.compile(r"A2UI component is not in catalog [^:]+: (\S+)$")

#: One default, actionable "what to do instead" hint per refusal reason —
#: filled in only when a caller does not supply its own (non-empty) hint, so
#: the existing mechanical hints (``catalog_hint``/``component_hint``, the
#: exact ``load_skill(...)`` call) still win. Every entry states the fastest
#: correct next step for a model reading this result, not a retry of the
#: identical call.
_DEFAULT_HINTS: dict[str, str] = {
    "a2ui_session_unavailable": (
        "this tool call ran outside an active GACT session turn; it cannot "
        "succeed standalone, do not retry it here"
    ),
    "a2ui_session_not_found": (
        "the session_id this call resolved to no longer exists on this "
        "server; do not retry with the same session"
    ),
    "a2ui_transcript_frozen": (
        "this turn's transcript ledger is already settled; do not retry in "
        "this turn, a later turn can persist again"
    ),
    "a2ui_surface_not_found": (
        "reuse a live id from a prior result's session_surface_ids, or call "
        "create_a2ui_surface to make a new surface"
    ),
    "a2ui_catalog_unknown": (
        "call create_a2ui_surface with an empty catalog_id to auto-select "
        "from this session's producible catalogs instead of naming one"
    ),
    "a2ui_catalog_not_producible": (
        "the active agent blueprint does not declare this catalog; call "
        "create_a2ui_surface with an empty catalog_id to auto-select one "
        "that is producible"
    ),
    # The reason named in the issue's live-gate evidence: 14 identical
    # refusals in one turn because this hint used to be empty.
    "a2ui_client_capabilities_unknown": (
        "this session's client renders no A2UI catalogs; answer in prose, do not retry"
    ),
    "a2ui_catalog_no_client_match": (
        "this session's client renders no catalog this session can "
        "produce; answer in prose, do not retry"
    ),
    "a2ui_no_catalogs_declared": (
        "this agent declares no A2UI catalogs, so it cannot create surfaces; "
        "answer in prose, do not retry"
    ),
    "a2ui_no_catalogs_resolved": (
        "none of this agent's declared A2UI catalogs could be loaded, so it "
        "cannot create surfaces; answer in prose, do not retry"
    ),
    "a2ui_preferred_catalog_not_selectable": (
        "omit catalog_id to auto-select instead, or pass one present in "
        "BOTH this result's client_supported_catalog_ids and "
        "producible_catalog_ids"
    ),
    # Fallback text for the two reasons that USUALLY get a computed
    # load_skill(...) hint (``catalog_hint``/``component_hint``) but can
    # still resolve to "" when the catalog itself is not resolvable — this
    # is what a caller then sees instead of a blank hint.
    "a2ui_function_not_in_catalog": (
        "call load_skill for this surface's catalog to see the functions it "
        "declares, then retry with a functionCall name it defines"
    ),
    "a2ui_validation_failed": (
        "call load_skill for the failing component's schema, then retry "
        "with a components payload that matches it"
    ),
}

#: Every refusal reason the producer package can emit through :func:`refusal`
#: (not through :class:`~clio_agent.gact.a2ui_capability_selection.
#: CatalogSelection`, which is exhaustively handled by
#: :func:`catalog_selection_refusal`). A completeness test
#: (``tests/test_gact/test_a2ui_producer.py``) asserts every one of these has
#: a non-empty default hint — a new reason added without wording it here is a
#: regression back to the un-actionable refusals issue #1374 fixed.
KNOWN_REFUSAL_REASONS: frozenset[str] = frozenset(
    {
        "a2ui_session_unavailable",
        "a2ui_session_not_found",
        "a2ui_transcript_frozen",
        "a2ui_catalog_unknown",
        "a2ui_catalog_not_producible",
        "a2ui_function_not_in_catalog",
        "a2ui_validation_failed",
        "a2ui_surface_not_found",
        "a2ui_client_capabilities_unknown",
        "a2ui_catalog_no_client_match",
        "a2ui_preferred_catalog_not_selectable",
    }
)


def refusal(reason: str, *, detail: str, hint: str = "") -> dict[str, Any]:
    """Build a typed producer-tool refusal dict (never raised as an exception).

    ``hint`` defaults to :data:`_DEFAULT_HINTS`'s entry for ``reason`` when
    the caller passes none — every reason ships an actionable default so a
    model never reads an empty hint on a mistake it could act on
    differently. Also records the SAME reason recurring within the current
    turn as a typed ``a2ui_producer_refusal_repeated`` ledger row
    (observability only; see the module docstring).
    """

    resolved_hint = hint or _DEFAULT_HINTS.get(reason, "")
    _record_repeat_within_turn(reason)
    return {"ok": False, "reason": reason, "detail": detail, "hint": resolved_hint}


def _record_repeat_within_turn(reason: str) -> None:
    """Record ``a2ui_producer_refusal_repeated`` when ``reason`` recurs in one turn.

    Best-effort: with no active app/session (e.g. ``a2ui_session_unavailable``
    itself, or a unit test that never wired one) this is a no-op — there is
    nowhere to durably record against, and ⚑ #1 forbids this module deciding
    anything from it either way.
    """

    app = _ctx.active_app()
    session_id = _ctx.active_session_id()
    if app is None or not session_id:
        return
    registry = getattr(app.state, "a2ui_catalogs", None)
    if registry is None:
        return
    registry.record_producer_refusal_reason(session_id, _ctx.active_turn_id(), reason)


def catalog_selection_refusal(
    selection: "CatalogSelection", *, preferred: str | None
) -> dict[str, Any]:
    """Build the fully-worded refusal for a failed :func:`select_catalog` outcome.

    ``create_a2ui_surface`` funnels five distinct selection failures through
    one call site; each reads its own ``detail`` stating exactly what is true
    about the session's negotiated capabilities rather than one reused
    generic sentence (S8, issue #1374 live-gate comment).
    """

    reason = selection.reason
    assert reason is not None, "catalog_selection_refusal requires a failed selection"
    if reason == "a2ui_no_catalogs_declared":
        detail = (
            "this session's agent declares no a2ui_catalogs, so it has no catalog "
            "to create a surface against"
        )
    elif reason == "a2ui_no_catalogs_resolved":
        detail = (
            "this session's agent declares a2ui_catalogs, but none of them "
            "resolved to a loadable catalog"
        )
    elif reason == "a2ui_client_capabilities_unknown":
        detail = (
            "this session's client has not advertised a2uiClientCapabilities "
            "yet, so no client catalog preference exists to select from"
        )
    elif reason == "a2ui_catalog_no_client_match":
        detail = (
            f"this session's client advertised supportedCatalogIds="
            f"{list(selection.client_supported_catalog_ids)!r}, none of which "
            f"are in this session's producible catalogs "
            f"{list(selection.producible_catalog_ids)!r}"
        )
    elif reason == "a2ui_preferred_catalog_not_selectable":
        detail = (
            f"catalog_id {preferred!r} is not in BOTH this session's "
            f"client-supported catalogs {list(selection.client_supported_catalog_ids)!r} "
            f"and its producible catalogs {list(selection.producible_catalog_ids)!r}"
        )
    else:  # pragma: no cover - exhaustive over select_catalog's own reasons
        detail = "catalog selection did not resolve a catalog for this new surface"
    return refusal(reason, detail=detail)


def component_from_validation_error(message: str) -> str:
    """Extract the failing component's name from an S2 validation message, or ``""``."""

    match = _COMPONENT_EQ_RE.search(message)
    if match:
        return match.group(1)
    match = _NOT_IN_CATALOG_RE.search(message)
    return match.group(1) if match else ""


def catalog_hint(app: Any, catalog_id: str) -> str:
    """The exact ``load_skill(...)`` call for a catalog's own guidance, or ``""``."""

    from clio_agent.gact.a2ui_catalogs.skills import catalog_skill_id  # noqa: PLC0415

    registry = getattr(app.state, "a2ui_catalogs", None)
    entry = registry.get(catalog_id) if registry is not None and catalog_id else None
    if entry is None:
        return ""
    return f'load_skill("{catalog_skill_id(entry)}")'


def component_hint(app: Any, catalog_id: str, message: str) -> str:
    """The exact ``load_skill(...)`` call for the failing component's schema.

    Falls back to the catalog-level hint when the failing component's name
    cannot be parsed out of ``message``, and to ``""`` when the catalog
    itself cannot be resolved.
    """

    from clio_agent.gact.a2ui_catalogs.skills import catalog_skill_id  # noqa: PLC0415

    registry = getattr(app.state, "a2ui_catalogs", None)
    entry = registry.get(catalog_id) if registry is not None and catalog_id else None
    if entry is None:
        return ""
    skill_id = catalog_skill_id(entry)
    component = component_from_validation_error(message)
    if component:
        return f'load_skill("{skill_id}", file="catalog.json#/components/{component}")'
    return f'load_skill("{skill_id}")'


__all__ = [
    "KNOWN_REFUSAL_REASONS",
    "catalog_hint",
    "catalog_selection_refusal",
    "component_from_validation_error",
    "component_hint",
    "refusal",
]
