"""Task-reference evidence forwarding on spawn (#1306 review round, finding 1).

The digest half (``agent_task_output_digest.py``) keeps the PARENT lean when it
collects an oversize completed child's output. That alone forces the exact flow
#1306 named as motivating (a coordinator sending assembled researcher evidence
through an independent critic) back through the parent: it would have to
``get_agent_task_output`` every researcher and re-inline the sum right back into
its own context -- the SAME bloat, one hop later.

This module is the other half: a typed ``input_task_ids`` reference on
``spawn_agent_task`` / ``spawn_agents_parallel``'s per-spawn entries. The parent
never touches the text -- it passes ids; the runtime resolves each referenced
task's FULL stored output (the identical ``spawn_runtime._resolve_verbatim_output``
path ``wait_agent_tasks`` and ``get_agent_task_output`` already use) and appends it,
clearly labeled, to the CHILD's own task briefing. The material lands in the
child's OWN context, never the parent's -- which is the point: a critic gets full
researcher evidence without the coordinator ever holding it.

Validation is a reality check, not a decision: an id must belong to the SPAWNING
session's own task registry scope (it spawned that task) and have reached a
terminal status, or the spawn is refused typed (no child created) rather than
silently starting a critic with a missing/foreign/incomplete reference. Nothing
here decides what the child does with the evidence -- forwarding it is the whole
mechanism.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# The two structural tokens an evidence block's header/footer use. The output
# they wrap is UNTRUSTED (the motivating case is web content a researcher child
# fetched) -- see _sanitize_evidence_output (#1306 final review round, finding
# B1) for why matching text inside it must never reach the child briefing raw.
_EVIDENCE_DELIMITER_RE = re.compile(r"(?m)^-{2,}\s*(Evidence from|end evidence)\b")


def _sanitize_evidence_output(output: str) -> str:
    """Neutralize the evidence-block delimiter tokens inside untrusted output.

    Mirrors the repo's decided precedent for the SAME class of problem
    (``gact/enrichment.py``'s ``_sanitize_excerpt``, #948 S6 adversarial-review
    [5]): a child's stored output is untrusted content that may reflect a
    poisoned document/page/tool result. Embedded raw between
    ``--- Evidence from ... ---`` / ``--- end evidence (...) ---`` delimiters, a
    poisoned answer could emit its OWN ``--- end evidence (task_r1) ---`` line
    followed by a forged ``--- Evidence from <trusted-agent> ---`` header,
    attributing fabricated material to a sibling task inside the SAME briefing.
    This replaces the two STRUCTURAL prefixes the referenced task's own output
    must never control (a run of 2+ dashes immediately followed by "Evidence
    from" or "end evidence" at the start of a line) with a single dash, so
    embedded text can never forge a new frame boundary. Content is otherwise
    preserved verbatim -- this is a delimiter fix, not a content redaction.
    """

    return _EVIDENCE_DELIMITER_RE.sub(r"- \1", output)


def _evidence_block(task: Any, output: str, fallback_reason: str) -> str:
    """One clearly labeled evidence block: which child, which agent, which task.

    ``fallback_reason`` (#1306 final review round, finding N1) is
    ``_resolve_verbatim_output``'s typed degradation marker
    (``output_fallback_reason``) when the child's message is gone and ``output``
    is only the bounded durable excerpt, not the true full text -- folded into
    the header itself (``PARTIAL: <reason>``) so the child sees the material is
    incomplete instead of silently receiving a 2000-char stub labeled as full
    evidence. Empty when the resolve was clean.
    """

    agent_id = task.agent_ref.get("expert_id", "")
    header = (
        f"--- Evidence from {agent_id} (task {task.task_id}, child session {task.child_session_id}"
    )
    if fallback_reason:
        header += f", PARTIAL: {fallback_reason}"
    header += ") ---"
    return f"{header}\n{_sanitize_evidence_output(output)}\n--- end evidence ({task.task_id}) ---"


def resolve_input_task_evidence(
    app: Any,
    session_id: str,
    task_text: str,
    input_task_ids: Any,
) -> tuple[str, list[str]]:
    """Append each referenced task's full output to ``task_text`` as evidence.

    Validates ``input_task_ids`` itself BEFORE touching the registry (#1306
    final review round, finding N2): anything other than ``None`` or a genuine
    ``list`` of ``str`` ids is refused typed (``task_ref_malformed``) rather
    than iterated -- a bare string like ``"task_r1"`` would otherwise silently
    iterate its CHARACTERS as ids (the exact documented footgun
    ``tool_instrumentation.py``'s ``_wait_agent_tasks_call_metadata`` already
    guards for ``wait_agent_tasks``' own ``task_ids``), fabricating a
    ``task_ref_unknown`` refusal on the first character instead of naming the
    real problem.

    Each id is then validated BEFORE building anything: unknown, not this
    session's own, or not yet terminal each raise a typed :class:`SpawnError`
    (reasons ``task_ref_unknown`` / ``task_ref_not_yours`` /
    ``task_ref_not_terminal``) so the caller (``spawn_runtime._do_spawn``,
    inside its existing try/except SpawnError block) refuses the WHOLE spawn
    -- no child is ever created on a broken reference, and a batch sibling's
    slot still reconciles the same way any other refused spawn does.

    Args:
        app: The active CLIO app (carries ``agent_task_registry``).
        session_id: The SPAWNING session's id -- a referenced task must be one
            IT spawned (``task.parent_session_id == session_id``).
        task_text: The child's task briefing before evidence is appended.
        input_task_ids: Optional task ids (from the spawning session's own
            prior spawns) whose full output to forward. Anything besides
            ``None``/a list of strings is refused (see above).

    Returns:
        ``(task_text, [])`` unchanged when ``input_task_ids`` is empty/``None``.
        Otherwise ``(task_text + one labeled evidence block per referenced
        task, ids)`` -- ``ids`` is the validated, order-preserved id list, for
        the STARTED handoff Part's metadata (ids only, NEVER the forwarded
        text, so the parent's own transcript stays honest about an input
        existing without holding the material itself -- #1306 final review
        round, finding N4).

    Raises:
        SpawnError: on a malformed ``input_task_ids`` or the first invalid
            reference (see reasons above).
    """

    if input_task_ids is None:
        return task_text, []

    from clio_agent.gact.agents.invoker import SpawnError  # noqa: PLC0415

    if not isinstance(input_task_ids, list) or not all(
        isinstance(item, str) for item in input_task_ids
    ):
        raise SpawnError(
            f"input_task_ids must be a list of task id strings, got {input_task_ids!r} "
            "(a bare string would silently iterate its characters as ids)",
            reason="task_ref_malformed",
        )
    ids = [t for t in input_task_ids if t]
    if not ids:
        return task_text, []

    from clio_agent.gact.agents.spawn_runtime import (  # noqa: PLC0415
        _resolve_verbatim_output,
    )

    registry = app.state.agent_task_registry
    blocks: list[str] = []
    for tid in ids:
        task = registry.get(tid)
        if task is None:
            raise SpawnError(f"referenced task {tid!r} is unknown", reason="task_ref_unknown")
        if task.parent_session_id != session_id:
            raise SpawnError(
                f"referenced task {tid!r} was not spawned by this session",
                reason="task_ref_not_yours",
            )
        if not task.is_terminal:
            raise SpawnError(
                f"referenced task {tid!r} has not completed yet (status={task.status!r})",
                reason="task_ref_not_terminal",
            )
        output, markers = _resolve_verbatim_output(app, task)
        fallback_reason = markers.get("output_fallback_reason", "")
        if fallback_reason:
            # Never silent (no-silent-fallback): the child receives the
            # degradation IN the block header, and the parent's own log
            # carries the structured fact too.
            logger.warning(
                "input_task_evidence degraded reason=%s task=%s child_session=%s",
                fallback_reason,
                task.task_id,
                task.child_session_id,
            )
        blocks.append(_evidence_block(task, output, fallback_reason))
    return task_text + "\n\n" + "\n\n".join(blocks), ids
