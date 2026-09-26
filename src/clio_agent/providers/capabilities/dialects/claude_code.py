"""Claude Code ``ThinkingSpec`` sourcing (model-capabilities brief Part 7 follow-up).

Claude Code is a Python SDK session, not an HTTP dialect -- but its per-model
effort evidence is real account/CLI truth
(:mod:`clio_agent.providers.model_discovery.claude_code_effort`, one
``initialize`` read, no model turn), and the CLI's own vocabulary
(``low``/``medium``/``high``/``xhigh``/``max``) is already CLIO's own level
vocabulary, so no translation table is needed here (unlike Codex's SDK
naming). This module only turns that already-discovered data into a
:class:`~clio_agent.providers.capabilities.records.ThinkingSpec`; the actual
CLI read lives in ``model_discovery.claude_code_effort``.

Every Claude model accepts SOME form of extended thinking -- a token budget at
minimum, even when the CLI reports no per-model effort levels (haiku) -- so
this never reports ``mechanism="none"``; that is a fact about the ``claude_code``
TRANSPORT (Anthropic's own thinking feature), not a per-model guess.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from clio_agent.providers.capabilities.records import Fact, ThinkingSpec, unknown

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _overlay_row(model: str) -> dict[str, Any] | None:
    """The claude_code refresh-overlay row for a configured model id or CLI alias.

    Moved from the deleted ``providers/reasoning_levels.py`` with the same
    logic (matches by canonical id OR any of the row's own ``cli_values``
    aliases the CLI reported at the last refresh).
    """

    from clio_agent.providers.model_discovery.overlay import (  # noqa: PLC0415
        OverlayMalformedError,
        read_overlay,
    )

    bare = model.removeprefix("claude_code/").removeprefix("cc-").removesuffix("[1m]")
    try:
        entry = read_overlay().get("claude_code")
    except OverlayMalformedError as exc:
        logger.warning("claude_code dialect: reason=overlay_malformed: %s", exc)
        return None
    rows = entry.get("models") if isinstance(entry, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        raw_aliases = row.get("cli_values")
        aliases = [str(a) for a in raw_aliases] if isinstance(raw_aliases, list) else []
        if bare == row.get("id") or bare in aliases or model in aliases:
            return row
    return None


def resolve_configured_model_id(model: str) -> str:
    """Return the full catalog model id a configured claude_code ``model`` resolves to.

    e.g. ``sonnet`` -> ``claude-sonnet-5``. Uses the SAME overlay row lookup
    the model's thinking/capability evidence comes from
    (:func:`_overlay_row`) -- the provider's own resolution, not a hand-typed
    alias table. Falls back to ``model`` unchanged when no overlay row
    matches (no discovery has run yet, or an unknown alias).
    """

    if not model:
        return model
    row = _overlay_row(model)
    resolved = str((row or {}).get("id") or "")
    return resolved or model


def build_thinking_spec(raw: dict[str, Any]) -> Fact[ThinkingSpec]:
    """Build the ``ThinkingSpec`` fact from one discovered/overlay claude_code row.

    ``raw`` carries ``supported_effort_levels`` (already CLIO vocabulary) and
    ``effort_evidence_failure`` (set when the CLI's own catalog read failed or
    never ran, or does not list this model), attached by
    :mod:`.claude_code_effort` at discovery time and forwarded through the
    refresh overlay. A model the CLI lists with no effort levels (haiku) still
    gets a real ``budget_tokens`` spec -- the CLI PROVED it exists and accepts
    thinking, just not via the effort dial. Only a row with NO evidence at all
    (no discovery/refresh has run) stays unknown.
    """

    failure = str(raw.get("effort_evidence_failure") or "")
    levels_raw = raw.get("supported_effort_levels")
    if not isinstance(levels_raw, list):
        if failure:
            return unknown(f"claude_code effort evidence unavailable: {failure}")
        return unknown("no supported_effort_levels evidence (no claude_code catalog refresh yet)")
    levels = tuple(str(v) for v in levels_raw if str(v))
    if levels:
        effort_by_level = {level: level for level in levels}
        return Fact(
            ThinkingSpec(mechanism="effort_levels", levels=levels, effort_by_level=effort_by_level),
            "server_report",
            _now_iso(),
            "claude_code CLI initialize models[].supportedEffortLevels",
        )
    # No model-specific ceiling is known, so budget_range stays None and the
    # request builder falls back to CLIO's own generic level->budget ladder
    # (providers.thinking_levels.LEVEL_BUDGET) rather than an invented range.
    return Fact(
        ThinkingSpec(mechanism="budget_tokens"),
        "server_report",
        _now_iso(),
        "claude_code CLI lists this model with no per-model effort levels "
        "(e.g. haiku) -- thinking still works via a token budget",
    )


def shipped_default_effort(raw: dict[str, Any]) -> str:
    """The maintained catalog's own shipped-default level for this model, or ``""``.

    Data (``catalogs/claude-code-models.json``'s ``shipped_default_effort``
    field, forwarded through discovery/the overlay), never a per-model NAME
    heuristic -- see ``catalogs/claude-code-models.json`` for which models
    currently declare one and why.
    """

    return str(raw.get("shipped_default_effort") or "")


def shipped_default_effort_for_model(model_id: str) -> str:
    """The catalog's shipped-default level for ``model_id``, disk-cache only.

    Used at :class:`~clio_agent.config.LMProviderConfig` CONSTRUCTION time
    (synchronous, no network, no discovery pipeline) -- reads the maintained
    catalog's own last-good disk cache directly
    (:func:`~clio_agent.providers.model_discovery.claude_code_catalog.
    cached_claude_code_catalog`), the SAME zero-network-call contract
    :class:`~clio_agent.providers.handshake.noop.NoOpHandshake` makes. ``""``
    when the model is unknown, uncached, or declares no shipped default --
    never a guess, and never a per-model NAME match.
    """

    from clio_agent.providers.model_discovery.claude_code_catalog import (  # noqa: PLC0415
        cached_claude_code_catalog,
    )

    catalog, _error = cached_claude_code_catalog()
    if catalog is None:
        return ""
    # Resolve an alias (e.g. "sonnet") to its canonical catalog id first, via
    # the SAME overlay lookup the rest of this module uses -- the static
    # catalog document only ever lists canonical ids.
    bare = resolve_configured_model_id(model_id)
    bare = bare.removeprefix("claude_code/").removeprefix("cc-").removesuffix("[1m]")
    for row in catalog.models:
        if row.get("id") == bare:
            return shipped_default_effort(row)
    return ""


def shipped_default_thinking_level(
    provider: str, model: str, thinking_level: str | None, thinking_budget: int
) -> str | None:
    """Return ``thinking_level``, filled with the model's shipped default when unset.

    Owner-module home for :class:`~clio_agent.config.LMProviderConfig`'s
    ``__post_init__`` shipped-default rule (#895) -- kept out of ``config.py``
    itself since that file's line count is a one-way ratchet (#775). DATA
    (:func:`shipped_default_effort_for_model`), never a per-model NAME
    heuristic; applies only when the caller set neither an explicit level nor
    a budget, and only for claude_code (the only provider publishing a
    shipped default today) -- every other provider's construction never pays
    the disk-cache read at all.
    """

    if thinking_level is not None or thinking_budget or provider != "claude_code":
        return thinking_level
    return shipped_default_effort_for_model(model or "") or thinking_level


__all__ = [
    "build_thinking_spec",
    "resolve_configured_model_id",
    "shipped_default_effort",
    "shipped_default_effort_for_model",
    "shipped_default_thinking_level",
]
