"""Bounded model-lane digest for a completed child task's output (#1306).

``check_agent_tasks`` already returns only a bounded ``answer_excerpt``
(``turn_spawn.py``'s ``_ANSWER_EXCERPT_MAX = 2000``) -- never the full text.
``wait_agent_tasks`` instead honors the #880 verbatim contract: its
completion payload's ``output`` is the child's FULL answer, byte-for-byte,
ALWAYS. For a short (earthscope-class) answer that is the right call; for a
content-producing child (a multi-page research report) it means a
coordinator collecting N such children accumulates the SUM of every child's
full output inline in its OWN context -- proven live in #1306: a single
``wait_agent_tasks`` observation carrying two completed research answers
measured 30,546 chars, and a researcher child turn two waves later recorded
399,913 input tokens. The coordinator lost workflow state at that size.

This mirrors the ``_clio`` truncation-envelope shape
``tools/mcp_result_projection.py`` already uses for one oversize MCP tool
result, applied to the SAME failure class one layer up (bytes accumulate
ACROSS children collected into one context, not just within one call). It is
a DIFFERENT knob from that module's ``model_tool_result_chars`` (own key, own
consumer, own failure mode -- the house rule that module's own docstring
states), because it bounds a different thing: one MCP call's result vs. one
spawned child's full answer.

The digest never discards data: a durable reference (the child session id +
the id of the message holding the full text) rides the envelope, and
``get_agent_task_output`` (built here, registered alongside the spawn tools
in ``spawn_runtime.build_spawn_runtime_tools``) fetches it verbatim on
demand -- recoverability is mandatory, per #1306. Nothing here decides
anything FOR the model: the envelope only states what happened and how to
get the rest; the model chooses whether to fetch it.
"""

from __future__ import annotations

import json
from typing import Any

#: Typed reason on the digest envelope's ``_clio.reason`` (mirrors
#: ``mcp_result_projection.MODEL_TOOL_RESULT_TRUNCATED_REASON``'s naming).
AGENT_TASK_OUTPUT_OVERSIZE_REASON = "agent_task_output_oversize"

#: The fetch tool a digest envelope's ``fetch_full_output.tool`` points at
#: (built by :func:`build_agent_task_output_tool`).
FETCH_FULL_OUTPUT_TOOL = "get_agent_task_output"


def agent_task_output_digest_chars() -> int:
    """Character bound above which a completed child's ``output`` is digested
    instead of inlined verbatim into the PARENT's ``wait_agent_tasks`` result.

    Config: ``limits.agent_task_output_digest_chars`` /
    ``CLIO_AGENT_TASK_OUTPUT_DIGEST_CHARS`` (default 8000) -- the SAME
    ``conf.resolve`` seam ``limits.model_tool_result_chars`` /
    ``limits.tool_result_chars`` already use for tool-result sizing; this is
    a third, distinct knob (own key, own consumer), never one silently
    redirecting another.

    8000 is set from the #1306 evidence, not a round number. The proven-bad
    wait observation (two completed research children) measured 30,546 JSON
    chars total; the non-``output`` payload fields (ids, status, stage,
    workflow_state, message_ref) run a few hundred bytes per row, so each
    child's own ``output`` text alone accounts for roughly 14-15K of that --
    confirmed independently by this fix's own regression fixture (a
    400-line synthetic child answer measuring 14,397 chars, sized to match
    that same order of magnitude). 8000 sits comfortably below that (both
    observed children still digest) and comfortably above a short, factual
    earthscope/wildfire-class answer -- the class #1306 itself calls out as
    already fine today -- at 4x the durable ``answer_excerpt`` bound
    (``turn_spawn.py``'s ``_ANSWER_EXCERPT_MAX = 2000``) those short answers
    already fit inside many times over.
    """

    from clio_agent import conf  # noqa: PLC0415

    return conf.resolve(
        "limits.agent_task_output_digest_chars",
        env="CLIO_AGENT_TASK_OUTPUT_DIGEST_CHARS",
        default=8_000,
        cast=conf.as_int,
    )


def digest_agent_task_output(
    output: str,
    *,
    task_id: str,
    child_session_id: str,
    message_ref: str,
    answer_excerpt: str,
) -> str:
    """Bound ONE completed child's model-facing ``output``.

    Below :func:`agent_task_output_digest_chars`, ``output`` is returned
    UNCHANGED -- byte-identical, the #880 verbatim contract holds (today's
    passing short-answer flows, e.g. earthscope, never see a different
    value). Above it, returns a typed ``_clio`` digest envelope (mirroring
    the model-tool-result truncation envelope's shape) carrying the
    already-bounded ``answer_excerpt`` (reused verbatim rather than
    re-sliced -- it is the SAME durable excerpt ``turn_spawn.py`` already
    computed from the identical message) plus a DURABLE reference --
    ``child_session_id`` + ``message_ref`` -- and the name of the tool that
    resolves it. No data is discarded: the full text stays exactly where
    the #880 contract already put it (the child session's own message
    store); this only stops it being duplicated a second time into the
    parent's own context.

    Args:
        output: The child's full, verbatim final-message text.
        task_id: The completed task's id (``get_agent_task_output``'s input).
        child_session_id: The child session holding the full message.
        message_ref: The id of the message holding the full text.
        answer_excerpt: The task record's own bounded excerpt (already
            capped by ``turn_spawn.py``; reused verbatim rather than
            re-sliced ``output`` a second time).

    Returns:
        ``output`` unchanged when it fits under the bound, else the
        JSON-encoded digest envelope (itself a plain ``str``, matching
        ``output``'s own type on the wire).
    """

    cap = agent_task_output_digest_chars()
    if len(output) <= cap:
        return output
    envelope: dict[str, Any] = {
        "_clio": {
            "status": "digested",
            "reason": AGENT_TASK_OUTPUT_OVERSIZE_REASON,
            "original_chars": len(output),
            "excerpt_chars": len(answer_excerpt),
        },
        "answer_excerpt": answer_excerpt,
        "task_id": task_id,
        "child_session_id": child_session_id,
        "message_ref": message_ref,
        "fetch_full_output": {
            "tool": FETCH_FULL_OUTPUT_TOOL,
            "args": {"task_id": task_id},
        },
    }
    return json.dumps(envelope, ensure_ascii=False, sort_keys=True)


def get_agent_task_output_impl(app: Any, task_id: str) -> str:
    """Resolve ``task_id``'s full stored output, or a typed error row.

    Separated from :func:`build_agent_task_output_tool`'s closure so it is
    directly unit-testable without building the whole tool/context
    machinery. Reads the SAME registry + message re-read path
    ``wait_agent_tasks`` itself uses (``spawn_runtime._resolve_verbatim_output``)
    -- one recoverability path, not a second parallel one.
    """

    registry = getattr(app.state, "agent_task_registry", None)
    task = registry.get(task_id) if registry is not None else None
    if task is None:
        return json.dumps({"error": "unknown_task", "task_id": task_id}, sort_keys=True)
    if not task.is_terminal:
        return json.dumps(
            {"error": "task_not_terminal", "task_id": task_id, "status": task.status},
            sort_keys=True,
        )

    from clio_agent.gact.agents.spawn_runtime import _resolve_verbatim_output  # noqa: PLC0415

    output, markers = _resolve_verbatim_output(app, task)
    result: dict[str, Any] = {"task_id": task_id, "status": task.status, "output": output}
    result.update(markers)
    return json.dumps(result, sort_keys=True)


def build_agent_task_output_tool() -> Any:
    """Build the ``get_agent_task_output`` dspy.Tool (#1306 recoverability).

    Agent story: a parent collects a "digested" (oversize) child result from
    ``wait_agent_tasks`` and wants the full text the envelope's
    ``fetch_full_output`` points at -- e.g. to quote it verbatim in its own
    deliverable, or hand it to the NEXT child as an input reference. This is
    the ONE lean tool that resolves a completed task's full stored output;
    it decides nothing FOR the model (no auto-injection, no summarization)
    -- the model asks for it only when it actually needs the full text.

    Bound into the spawn-runtime toolset (registered alongside the other
    collector tools in ``spawn_runtime.build_spawn_runtime_tools``, same
    gating as ``wait_agent_tasks``/``check_agent_tasks``).
    """

    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    def get_agent_task_output(task_id: str) -> str:
        """Fetch a completed task's FULL stored output, byte-for-byte.

        Use this after wait_agent_tasks returns a "digested" envelope
        (output too large to inline) -- the envelope's fetch_full_output
        names this tool. Returns a typed error for an unknown task id or one
        that has not reached a terminal status yet."""

        app = _ctx.active_app()
        if app is None:
            raise RuntimeError("get_agent_task_output requires an active CLIO app context")
        return get_agent_task_output_impl(app, task_id)

    return native_tool(
        get_agent_task_output,
        name=FETCH_FULL_OUTPUT_TOOL,
        desc=get_agent_task_output.__doc__,
        title="Get Task Output",
        args={
            "task_id": {
                "type": "string",
                "description": "A completed task's id (from spawn/wait/check).",
            },
        },
    )
