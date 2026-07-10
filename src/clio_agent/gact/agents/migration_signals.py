"""Blueprint config-migration signals (owner module for the #736 unify seam).

A NEW owner module (no-accretion: nothing bolted onto the already-ratcheted
``builders.py``). It holds the ONE-TIME, construction-path detector for a
config-migration break the #736 ``final_responder`` unify introduced, plus its
dedicated typed reason catalog and per-app ledger.

Background — the break this closes
-----------------------------------
Before #736 an expert's answer live-stream visibility was
``_agent_id == "synthesis" or not workflow_state``: an expert literally named
``synthesis`` always streamed its answer, and so did any expert that did not run
the typed-state engine. #736 replaced that with the declarative
``structured_outputs.final_responder`` flag (correct — no name heuristic on prose,
principle #1). But a THIRD-PARTY pack whose expert is literally named
``synthesis``, declares ``workflow_state: true``, and does NOT add
``final_responder: true`` silently flips from VISIBLE to HIDDEN: the turn stops
streaming its answer and the author gets no signal.

We do NOT restore the name-based behaviour (that would re-introduce the
principle-#1 hardcode). Instead this module DETECTS exactly that un-migrated
shape at module-construction time and records a loud, always-on, queryable
structured reason (the unified turn-degradation ledger + a ``logging.warning``
naming the expert and the one-line migration) so the flip stops being silent.
Detecting the shape by the declarative expert id + declared flags is DATA
inspection, not a routing decision, and the record is emitted once per
construction (cheap — never a per-token check).

The record goes to the unified ``app.state.turn_degradations`` ledger
(:mod:`clio_agent.gact.turn_degradation`, reason ``final_responder_flag_absent``,
category ``config_migration``): always-on (never gated on
``CLIO_STREAM_AUDIT_LOG``), per session, capped and consecutive-dedup'd, and
DRAINED at finalize onto the assistant message metadata so the signal reaches the
trace/API rather than sitting in a write-only ledger.

Reachability of that drain (traced, not hand-waved):
:func:`check_final_responder_migration` is called from
``BlueprintExpertModule.__init__`` (``agents/builders.py``), constructed fresh per
forward under ``_ctx.set_session_id(state.sid)`` on BOTH the streamed
(``turn_forward``) and sync (``app._run_blueprint_dspy_agent``) paths — so it
fires before finalize with the sid bound and CAN reach the message metadata. The
one path it cannot: an app-less sync build (session present, ``_ACTIVE_GACT_APP``
is ``None``) or genuine out-of-turn construction (catalog preview / blueprint
validation, no sid) — there is NO turn message to attach to, so the honest
always-on sink there is the ``logging.warning`` below (``record_turn_degradation``
cannot persist an app-less record, and warns rather than dropping it silently).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.runtime.type_parsing import _structured_output_enabled
from clio_agent.gact.turn_degradation import _turn_degradation_payload, record_turn_degradation

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import AgentDef

logger = logging.getLogger(__name__)

# The migration-sensitive expert id. Kept as DATA (a declarative blueprint id),
# not a keyword match on model prose — it names the exact pre-#736 shape whose
# stream visibility silently flipped, so the flip can be surfaced, never decided.
_MIGRATION_SENSITIVE_AGENT_ID = "synthesis"

# The unified turn-degradation reason for this seam (validated + drained by
# :mod:`clio_agent.gact.turn_degradation`).
FINAL_RESPONDER_MIGRATION_REASON = "final_responder_flag_absent"


def _is_unmigrated_synthesis(agent_def: "AgentDef") -> bool:
    """True iff ``agent_def`` is the exact un-migrated pre-#736 shape.

    The shape whose answer-stream visibility silently flipped: expert id is
    literally ``synthesis`` AND ``workflow_state`` is enabled AND
    ``final_responder`` is NOT enabled (absent or falsy). All three are read as
    declarative blueprint DATA via the shared truthiness helper, so a quoted
    author error (``"no"``/``"false"``) cannot masquerade as the flag being set.
    """

    if str(getattr(agent_def, "id", "") or "") != _MIGRATION_SENSITIVE_AGENT_ID:
        return False
    structured = getattr(agent_def, "structured_outputs", None)
    structured = structured if isinstance(structured, Mapping) else {}
    workflow_enabled = _structured_output_enabled(structured.get("workflow_state") or False)
    final_responder_enabled = _structured_output_enabled(structured.get("final_responder") or False)
    return workflow_enabled and not final_responder_enabled


def check_final_responder_migration(
    app: "FastAPI | None",
    sid: str,
    agent_def: "AgentDef",
) -> dict[str, Any] | None:
    """Detect the un-migrated #736 ``synthesis`` shape and record a loud signal.

    A one-time construction-path check: when ``agent_def`` matches the exact shape
    whose answer-stream visibility silently flipped from visible to hidden under
    #736 (see :func:`_is_unmigrated_synthesis`), emit a structured, always-on
    reason — recorded on the unified ``turn_degradations`` ledger (drained at
    finalize onto the assistant message, queryable after the fact) and logged at
    WARNING (immediate feedback naming the expert and the migration). Any other
    shape is a no-op; an app-less / session-less caller still logs + returns the
    payload but records nothing (``record_turn_degradation`` no-ops — there is no
    turn message to attach to).

    Args:
        app: The live GACT app carrying the per-session ledger (``None`` when
            app-less — nothing to attribute, so the signal is logged only).
        sid: The active session id (empty when session-less).
        agent_def: The blueprint expert being constructed.

    Returns:
        The recorded typed payload when the un-migrated shape fired, else ``None``.
    """

    if not _is_unmigrated_synthesis(agent_def):
        return None
    agent_id = str(getattr(agent_def, "id", "") or "")
    source = str(getattr(agent_def, "source", "") or "unknown")
    message = (
        f"expert {agent_id!r} (source={source}) declares workflow_state but not "
        "final_responder -- its answer stream is now HIDDEN; add "
        "'final_responder: true' to its structured_outputs to restore visible "
        "streaming (#736 migration)"
    )
    payload = _turn_degradation_payload(FINAL_RESPONDER_MIGRATION_REASON, message)
    # WARNING gives the pack author immediate, default-deployment feedback; the
    # unified ledger below keeps the same signal queryable after the fact (no-op
    # when app-less — nothing to attribute).
    logger.warning("final_responder migration required: %s", message)
    record_turn_degradation(app, sid, FINAL_RESPONDER_MIGRATION_REASON, message)
    return payload
