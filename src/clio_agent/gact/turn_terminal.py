"""Terminal-responder settle logic for the GACT delegation engine (#736).

The delegation settle loop (:func:`clio_agent.gact.turn_delegation.
settle_dynamic_agent_delegations`) re-invokes the parent orchestrator after
*every* completed child round so the parent can emit its next route (descend
again, or finish). That extra round is correct for intermediate orchestrators,
but WRONG after the pipeline's declared final responder runs: the final
responder's answer already *is* the user-facing deliverable, so re-invoking the
parent produces a redundant restatement that overwrites the deliverable (#736).

The fix is declarative, not a prose heuristic (superseding principle #1): a
child declares itself the turn's final responder with ``final_responder: true``
inside its ``structured_outputs:`` frontmatter block. When such a child completes
a round, the settle loop ADOPTS its answer as the turn deliverable and stops —
the parent is not re-invoked. The MODEL still routes (it emits
``next_expert=synthesis``); the flag only asserts "this child's answer is the
user deliverable", so the redundant post-terminal parent re-invoke is skipped.

Why a declarative key is unavoidable: a parent's children resolve as an
*unordered set* — there is no portable structural signal for "which child is
last", ordering is not encoded, and the only prior signal was a hardcoded
``id == "synthesis"`` string literal (a smell this change removes). The static
flag is DATA on ``AgentDef.structured_outputs``, read through one predicate,
NEVER from ``next_expert``, ``.answer``, ``.reasoning``, the child id, or child
ordering.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from clio_agent.gact.delegation import _prediction_workflow_state
from clio_agent.gact.runtime.type_parsing import _structured_output_enabled
from clio_agent.gact.turn_degradation import record_turn_degradation
from clio_agent.gact.workflow_state.merge import _merge_workflow_state_mapping
from clio_agent.runtime.stream_audit import stream_audit

if TYPE_CHECKING:
    from clio_agent.gact.turn_state import TurnState
    from clio_agent.gact.types import AgentDef
    from clio_agent.gact.workflow_state.schema import WorkflowStateSchema


def is_final_responder(agent_def: "AgentDef") -> bool:
    """True iff the child declares ``structured_outputs.final_responder`` (declarative data).

    Args:
        agent_def: The child expert definition to inspect.

    Returns:
        ``True`` when the child's ``structured_outputs`` mapping carries a truthy
        ``final_responder`` flag; ``False`` otherwise (absent / not a mapping).
    """

    # Route the read through the SAME structured-flag truthiness helper the signature
    # builder uses (#736/C), so a QUOTED author error (``final_responder: "no"`` /
    # ``"false"``) is treated as DISABLED — ``bool("no")`` is truthy, so a raw ``bool``
    # would silently ENABLE a quoted-off flag and short-circuit a turn. The ``or False``
    # coalesces the OFF-by-default falsy values (absent/``None``/``""``/``0``) so only a
    # real truthy value or a non-disabled string reaches the helper.
    so = getattr(agent_def, "structured_outputs", None) or {}
    if not isinstance(so, Mapping):
        return False
    return _structured_output_enabled(so.get("final_responder") or False)


def final_responder_ids(child_defs: list["AgentDef"]) -> frozenset[str]:
    """Ids of the parent's declared children flagged ``final_responder``.

    Args:
        child_defs: The parent's declared child expert definitions.

    Returns:
        The frozen set of child ids whose definition declares the flag.
    """

    return frozenset(d.id for d in child_defs if is_final_responder(d))


def adopt_final_responder_answer(
    parent_pred: Any,
    completed_row: dict[str, Any],
    child_id: str,
    *,
    schema: "WorkflowStateSchema",
) -> Any:
    """Extract the terminal child's answer as the turn deliverable.

    Copies the parent's routing prediction (preserving ``execution_path``,
    ``file_diffs``, ``nanoagents``, ``permissions`` and other main-only fields),
    overrides the user-facing ``answer`` and ``workflow_state``, and sets
    ``selected_expert=child_id`` so ``finalize`` attributes the turn's answer to the
    child. That attribution is load-bearing: it flips ``responder_agent_id`` to the
    child so finalize's ``turn_answer_stream`` op-identity dedup collapses the
    server-side double to exactly one answer part, against the answer the child
    already landed at its LM-call site (``turn_delegation.py`` completed-row build).

    It also DROPS the parent's ``reasoning`` / ``trajectory`` (#736/D): because the
    deliverable is now attributed to the child, ``turn.py`` sets
    ``state.thinking_text = pred.reasoning`` (falling back to the formatted
    ``trajectory``) and ``turn_finalize`` appends it as a ``thinking`` Part under
    ``responder_agent_id`` — the child. Carrying the parent's routing rationale
    ("route to synthesis") would surface MAIN's disclosure MISLABELED as the child's
    reasoning (and duplicate it: it already appears once as the delegate.started
    handoff thought). The child's own reasoning, when it has any, streamed live under
    its own scope during execution — it is not re-derived from this envelope.

    Args:
        parent_pred: The parent's most recent routing prediction (the envelope
            whose main-only fields we preserve).
        completed_row: The terminal child's completed delegation row.
        child_id: The terminal child's id (attributed as ``selected_expert``).
        schema: The active pack workflow_state schema (drives the merge).

    Returns:
        A prediction whose ``answer`` is the child's deliverable, ``selected_expert``
        is ``child_id``, ``next_expert`` is ``"finish"``, and ``workflow_state`` is
        the parent's state merged with the child's returned state.
    """

    answer = str(
        completed_row.get("output")
        or completed_row.get("output_raw")
        or completed_row.get("output_summary")
        or ""
    ).strip()
    pred = parent_pred.copy() if hasattr(parent_pred, "copy") else parent_pred
    pred.answer = answer
    pred.selected_expert = child_id
    pred.next_expert = "finish"
    # #736/D: strip the parent's disclosure so it is not relabeled as the CHILD's
    # thinking (state.thinking_text = pred.reasoning or _format_react_trajectory(...)).
    pred.reasoning = ""
    if getattr(pred, "trajectory", None):
        pred.trajectory = {}
    child_ws = completed_row.get("workflow_state")
    if isinstance(child_ws, Mapping) and child_ws:
        merged = _prediction_workflow_state(parent_pred, schema=schema)
        _merge_workflow_state_mapping(merged, child_ws, schema=schema)
        pred.workflow_state = merged
    return pred


async def settle_parent_next_pred(
    state: "TurnState",
    parent_agent: "AgentDef",
    source_text: str,
    all_rows: list[dict[str, Any]],
    completed_this_round: list[dict[str, Any]],
    declared_child_ids: set[str],
    final_ids: frozenset[str],
    latest_pred: Any,
    *,
    schema: "WorkflowStateSchema",
) -> tuple[Any, bool]:
    """Decide the parent's next prediction after a completed child round.

    If the completed child is the declared final responder, adopt its answer as
    the turn deliverable and stop (no parent re-invoke). Otherwise build the
    resume prompt and re-invoke the parent, exactly as before — so intermediate
    orchestrators (and their own non-terminal children) still route legitimately.

    Args:
        state: The active turn state (session/turn identity, ARC wiring).
        parent_agent: The orchestrator whose children just ran.
        source_text: The parent's original input (resume-prompt seed).
        all_rows: All handoff rows accumulated this settle so far.
        completed_this_round: The rows completed in the round just finished.
        declared_child_ids: The parent's declared child ids.
        final_ids: The subset of ``declared_child_ids`` flagged ``final_responder``.
        latest_pred: The parent's most recent routing prediction.
        schema: The active pack workflow_state schema.

    Returns:
        ``(pred, should_break)``: the next prediction to carry, and whether the
        settle loop should stop (``True`` only when a final responder settled).
    """

    # Find a completed final-responder row (static-flag match only — never from
    # next_expert, the child id, or child ordering).
    final_row = None
    child_id = ""
    for row in completed_this_round:
        cid = str(row.get("agent_id") or row.get("delegate_to") or "").strip()
        if cid in final_ids:
            final_row, child_id = row, cid
            break
    if final_row is not None:
        # STRUCTURAL channel read (never a prose-content sniff — #736/B): the completed
        # row is BUILT with the child's prose answer on ``output`` and its typed
        # ``dspy.extract`` (JSON) answer on ``output_raw``, with the other blanked
        # (see turn_delegation completed-row build). So the answer's SHAPE is known from
        # WHICH channel carried it, not from inspecting the text — a prose answer that
        # merely opens with '{' is still prose. The reason label is cosmetic (adopt/stop
        # are identical either way); keeping it structural keeps a future change from
        # quietly hanging a real decision on a content heuristic.
        output_text = str(final_row.get("output") or "").strip()
        output_raw = str(final_row.get("output_raw") or "").strip()
        summary_text = str(final_row.get("output_summary") or "").strip()
        answer = output_text or output_raw or summary_text
        if not answer:
            # No-silent-fallback: the terminal child returned nothing. Still end
            # the settle (do not paper over emptiness by re-invoking the parent)
            # and emit a structured reason; finalize surfaces the delegation
            # evidence as the fallback answer.
            reason = "final_responder_empty_answer"
            # ALWAYS-ON ledger: the stream_audit call below is the high-detail trace
            # but is gated on CLIO_STREAM_AUDIT_LOG (default "" -> writes nothing in a
            # default deployment). Record the same degradation on the unified per-session
            # ``app.state.turn_degradations`` ledger (drained onto the assistant message
            # at finalize) so the substituted downgrade the user sees stays queryable
            # after the fact, independent of that gate.
            record_turn_degradation(
                state.app,
                state.sid,
                reason,
                f"parent={parent_agent.id} child={child_id}",
            )
        elif output_raw and not output_text:
            # Answer rode the typed ``output_raw`` channel: the structured deliverable.
            reason = "final_responder_structured_answer"
        else:
            reason = "final_responder_settled"
        stream_audit(
            "delegation.final_responder_settled",
            session_id=state.sid,
            turn_id=state.turn_id,
            agent_id=parent_agent.id,
            child_id=child_id,
            reason=reason,
            visible_answer=bool(answer),
        )
        return (
            adopt_final_responder_answer(latest_pred, final_row, child_id, schema=schema),
            True,
        )

    # Function-local imports break the module cycle (turn_delegation imports this
    # module at top level; these two would cycle back).
    from clio_agent.gact.delegation import _dynamic_parent_resume_prompt  # noqa: PLC0415
    from clio_agent.gact.turn_delegation import run_dynamic_agent_sync  # noqa: PLC0415

    # Re-invoke the parent with the child's returned evidence so IT emits the next
    # route (descend again, or finish). (Explanatory comment MOVED, not deleted,
    # from the former settle-loop resume block in turn_delegation.py.)
    resume_prompt = _dynamic_parent_resume_prompt(
        source_text,
        parent_agent,
        all_rows,
        declared_child_ids=declared_child_ids,
        schema=schema,
    )
    return await run_dynamic_agent_sync(state, parent_agent, resume_prompt), False
