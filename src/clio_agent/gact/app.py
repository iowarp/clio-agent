"""GACT v0.2 FastAPI application for CLIO.

Exposes the GACT v0.2 contract surface. Most routes are 501 stubs
today (CLIO-BBBBBBBBBB6); they get wired one at a time in
follow-on iterations (BBB7–BBB12) against the spec at
``gact-tui/contract/SPEC.md`` and the docs in ``docs/tui/``.

Run via::

    clio-agent-gact --host 127.0.0.1 --port 8100

Or::

    uvicorn clio_agent.gact.app:app --host 127.0.0.1 --port 8100

This is a peer of ``clio_agent.ui.api`` (the native CLIO REST API),
not a replacement — both can run side-by-side. The TUI integration
target is the GACT app; existing CLI + direct-Python callers keep
using the native API unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager, nullcontext as _nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse


def _format_sse(event: "Event") -> bytes:
    """Render an Event as the SSE wire format (SPEC §7.2)::

        event: <type>
        id: <numeric monotonic id>
        data: <json envelope>
        <blank line>
    """

    payload = json.dumps(event.envelope())
    lines = (
        f"event: {event.type}\n"
        f"id: {event.id}\n"
        f"data: {payload}\n\n"
    )
    return lines.encode("utf-8")

# ---- ID + timestamp helpers used by the message endpoint ---------
# Kept at module level (not inside build_app) so they're trivially
# importable by future streaming code + easy to mock in tests.


def _new_message_id(role_prefix: str) -> str:
    """Generate a message id. Role prefix ('user' / 'asst' / 'tool')
    makes log scraping + human triage cheaper."""

    return f"msg_{role_prefix}_{uuid.uuid4().hex[:12]}"


def _new_part_id() -> str:
    return f"part_{uuid.uuid4().hex[:12]}"


def _iso_from_epoch(ts: float) -> str:
    """ISO-8601 UTC with microsecond precision to match the session
    registry's created_at format."""

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


async def _run_turn_in_background(
    app: "FastAPI",
    sid: str,
    user_text: str,
    user_msg: "Message",
) -> None:
    """Drive an agent turn off the request thread.

    The POST handler returns immediately after staging the user
    message; this coroutine handles the rest: invoking forward() in
    an executor, slicing the result into Parts, publishing every
    SSE event the TUI consumes, persisting the assistant message,
    and settling the session back to idle (or error).

    Errors here are *consumed* — they emit a message.completed with
    error_info and a session.status_changed → error so the TUI sees
    the failure live. We never re-raise; the request that started us
    is long gone.
    """

    bus: EventBus = app.state.bus
    sess = app.state.sessions.get(sid)
    if sess is None:
        # Session evaporated between POST + background start; can't
        # do anything useful. Don't raise — the publishing path
        # would crash and pollute logs with no client to notify.
        return

    error_info: Optional[ErrorInfo] = None
    answer_text = ""
    selected_agent = ""
    rationale = ""
    tools_called: list[dict[str, Any]] = []
    proposed_diffs: list[Any] = []
    nanoagents: list[Any] = []
    thinking_text = ""
    turn_tokens: dict[str, int] = {
        "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
    }
    turn_cost = 0.0

    # iowarp/clio-agent#5: prepend any attached context files to the
    # user's text so the agent's forward() sees them as primed input.
    # Plain text concat — keeps the agent.py interface untouched and
    # works regardless of which expert handles the turn.
    enriched_text = _enrich_with_context_files(
        app, sid, user_text
    )
    # iowarp/clio-agent#20: pre_message hook can transform the
    # input or veto the turn. PermissionError → cancelled-style
    # error_info; the caller sees the hook's reason.
    try:
        from clio_agent.runtime.hooks import fire as _fire_hook

        _fire_hook("pre_message", sid, enriched_text)
    except PermissionError as exc:
        bus.publish(Event(
            type="message.completed",
            session_id=sid,
            payload={
                "message_id": user_msg.id,
                "stop_reason": "blocked",
                "error_info": {
                    "error": "permission_error",
                    "message": str(exc),
                    "recoverable": True,
                },
            },
        ))
        app.state.sessions.update(sid, status="error")
        bus.publish(Event(
            type="session.status_changed",
            session_id=sid,
            payload={
                "session_id": sid,
                "status": "error",
                "prev_status": "running",
                "reason": "pre_message hook blocked turn",
            },
        ))
        return

    # iowarp/clio-agent#6: try real per-token streaming via
    # dspy.streamify when the LM supports it; fall back to the
    # synchronous executor path otherwise. Streaming produces
    # message.part.delta events as chunks arrive — without it the
    # text part lands as one big delta after forward returns.
    streamed_assistant_part_id: Optional[str] = None
    streamed_assistant_buffer: list[str] = []
    streamed_assistant_msg_id: Optional[str] = None

    async def _emit_chunk(text: str) -> None:
        nonlocal streamed_assistant_part_id, streamed_assistant_msg_id
        if streamed_assistant_msg_id is None:
            # Lazily invent ids the moment the first chunk arrives;
            # the final assistant message will reuse them.
            streamed_assistant_msg_id = _new_message_id("asst")
            streamed_assistant_part_id = _new_part_id()
            bus.publish(Event(
                type="message.created",
                session_id=sid,
                payload=Message(
                    id=streamed_assistant_msg_id,
                    session_id=sid,
                    role="assistant",
                    created_at=_iso_from_epoch(time.time()),
                    updated_at=_iso_from_epoch(time.time()),
                    parts=[],
                ).model_dump(exclude_none=True),
            ))
            bus.publish(Event(
                type="message.part.added",
                session_id=sid,
                payload={
                    "message_id": streamed_assistant_msg_id,
                    "part": Part(
                        id=streamed_assistant_part_id,
                        type="text",
                        text="",
                    ).model_dump(exclude_none=True),
                },
            ))
        streamed_assistant_buffer.append(text)
        bus.publish(Event(
            type="message.part.delta",
            session_id=sid,
            payload={
                "message_id": streamed_assistant_msg_id,
                "part_id": streamed_assistant_part_id,
                "delta": {"text_append": text},
            },
        ))

    # iowarp/clio-agent#8: snapshot LM history before the turn so we
    # can sum every call this turn made. ContextVars don't propagate
    # to asyncio executor threads (so dspy.settings.usage_tracker is
    # unreliable from worker threads), but ``lm.history`` IS shared
    # across threads — list.append under the GIL gives us a clean,
    # thread-safe ledger. We diff history[start:end] post-turn.
    history_start = _snapshot_lm_history_index(app)

    try:
        # Honour the session's routing override. routing_mode "chat"
        # forces the chat path (no /chat prefix needed); "experts"
        # rejects chat/none classifications. We mutate the agent's
        # ProviderConfig.routing_mode for the duration of the call so
        # ClioAgent.forward can see it without changing its public
        # signature.
        routing_override = getattr(sess, "routing_mode", "auto") or "auto"
        agent_obj = app.state.agent
        prev_routing = getattr(agent_obj, "_routing_mode_override", "auto")
        try:
            agent_obj._routing_mode_override = routing_override  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

        pred = await _try_streamed_forward(
            app, enriched_text, sid, _emit_chunk,
            session_mode=getattr(sess, "mode", "chat"),
            session_edit_mode=getattr(sess, "edit_mode", "diff"),
        )
        if pred is None:
            loop = asyncio.get_running_loop()
            pred = await loop.run_in_executor(
                None,
                lambda: _agent_forward_compat(
                    app.state.agent,
                    enriched_text,
                    sid,
                    getattr(sess, "mode", "chat"),
                    getattr(sess, "edit_mode", "diff"),
                ),
            )
        try:
            agent_obj._routing_mode_override = prev_routing  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        answer_text = getattr(pred, "answer", "")
        selected_agent = getattr(pred, "selected_expert", "") or ""
        rationale = getattr(pred, "routing_rationale", "")
        tools_called = _extract_tools_called(pred)
        # Drain the per-session observer ledger so direct-tool short-
        # circuits (HDF5/Parquet/fs experts that bypass ReAct) still
        # report tools_called on the assistant message metadata.
        ledger = getattr(app.state, "tool_call_ledger", None)
        if ledger is not None:
            observed = ledger.pop(sid, [])
            if observed and not tools_called:
                tools_called = observed
            elif observed:
                # Both populated — the expert's own list takes
                # precedence (richer payload), but append any
                # observed-only calls the expert didn't enumerate.
                seen = {(t.get("name"), str(t.get("args"))) for t in tools_called}
                for o in observed:
                    if (o.get("name"), str(o.get("args"))) not in seen:
                        tools_called.append(o)
        # iowarp/clio-agent#17 — surface DSPy reasoning as a
        # `thinking` Part. ChainOfThought predictions expose
        # ``.reasoning`` (single string); ReAct exposes
        # ``.trajectory`` (step-by-step trace). Fall back to the
        # generic `_trace` Prediction wraps either of them in.
        thinking_text = (
            getattr(pred, "reasoning", "")
            or _format_react_trajectory(getattr(pred, "trajectory", None))
            or ""
        )
        # CLIO-BBBBBBBBBB24: cost + token rollup. Real DSPy
        # predictions don't always populate .tokens / .cost_usd
        # directly — pull from the per-turn UsageTracker first
        # (works across threads + streaming), then LM history.
        raw_tokens = getattr(pred, "tokens", None)
        if raw_tokens is not None:
            for key in turn_tokens:
                if isinstance(raw_tokens, dict):
                    v = raw_tokens.get(key, 0)
                else:
                    v = getattr(raw_tokens, key, 0)
                turn_tokens[key] = int(v or 0)
        else:
            # Diff the LM history slice for this turn first — captures
            # router + expert + chat calls cleanly. Falls back to
            # ``last entry only`` for older code paths, then to a
            # character-based estimate when the upstream proxy
            # reports zero (Meridian quirk on some chunked replies).
            history_end = _snapshot_lm_history_index(app)
            history_made_calls = any(
                history_end.get(k, 0) > history_start.get(k, 0)
                for k in {*history_start.keys(), *history_end.keys()}
            )
            usage = _usage_from_history_slice(history_start, app)
            if not usage.get("output"):
                usage = _usage_from_dspy_history()
            for key in turn_tokens:
                turn_tokens[key] = int(usage.get(key, 0) or 0)
            turn_cost = float(usage.get("cost_usd", 0.0) or 0.0)
            # Char-based fallback only when the LM actually fired
            # this turn (history grew) but the upstream proxy
            # reported zero usage. Don't synthesize numbers when
            # there was no real call (e.g. unit tests with a fake
            # agent that bypasses dspy.LM entirely).
            if history_made_calls:
                if turn_tokens["output"] == 0 and answer_text:
                    turn_tokens["output"] = max(1, len(answer_text) // 4)
                if turn_tokens["input"] == 0 and enriched_text:
                    turn_tokens["input"] = max(1, len(enriched_text) // 4)
                if turn_cost == 0.0:
                    turn_cost = _estimate_cost_usd(
                        _current_lm_model_id(),
                        turn_tokens["input"], turn_tokens["output"],
                    )
        if not turn_cost:
            turn_cost = float(getattr(pred, "cost_usd", 0.0) or 0.0)
        proposed_diffs = list(getattr(pred, "file_diffs", None) or [])
        nanoagents = list(getattr(pred, "nanoagents_spawned", None) or [])
        for req in (getattr(pred, "permissions_requested", None) or []):
            src = req if isinstance(req, dict) else {
                "tool_call": getattr(req, "tool_call", {}),
                "summary": getattr(req, "summary", ""),
                "id": getattr(req, "id", ""),
            }
            pid = src.get("id") or f"perm_{uuid.uuid4().hex[:12]}"
            row = {
                "id": pid,
                "session_id": sid,
                "tool_call": src.get("tool_call") or {},
                "summary": src.get("summary", ""),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "pending",
            }
            app.state.permissions[pid] = row
            bus.publish(Event(
                type="permission.requested",
                session_id=sid,
                payload=row,
            ))
        if sid in app.state.cancel_flags:
            app.state.cancel_flags.discard(sid)
            error_info = ErrorInfo(
                error="cancelled",
                message="turn cancelled by client",
                details={"session_id": sid},
                recoverable=True,
            )
            answer_text = ""
            tools_called = []
    except asyncio.CancelledError:
        error_info = ErrorInfo(
            error="cancelled",
            message="turn cancelled by client",
            details={"session_id": sid},
            recoverable=True,
        )
        answer_text = ""
        tools_called = []
    except Exception as exc:  # noqa: BLE001
        error_info = ErrorInfo(
            error="agent_error",
            message=f"agent.forward raised: {exc}",
            details={"original_error": type(exc).__name__},
            recoverable=True,
        )

    # Build assistant parts — routing_decision (v0.2) first when we
    # got a selected_agent, then optional thinking trace, then the
    # text answer, then any file_diffs.
    assistant_parts: list[Part] = []
    if selected_agent:
        assistant_parts.append(Part(
            id=_new_part_id(),
            type="routing_decision",
            selected_agent=selected_agent,
            rationale=rationale,
            confidence=0.0,
            heuristic=False,
        ))
    if thinking_text:
        # iowarp/clio-agent#17: surface DSPy reasoning as a
        # thinking Part so the TUI can collapse + render it
        # gated on capabilities.thinking_blocks.
        assistant_parts.append(
            Part(id=_new_part_id(), type="thinking", text=thinking_text)
        )
    if answer_text:
        assistant_parts.append(
            Part(id=_new_part_id(), type="text", text=answer_text)
        )
    for row in proposed_diffs:
        if isinstance(row, dict):
            getf = row.get
        else:
            getf = lambda k, default=None, _r=row: getattr(_r, k, default)
        path = getf("path", "") or ""
        udiff = getf("unified_diff", "") or ""
        new_content = getf("new_content", "") or ""
        edit_mode = getf("edit_mode", "") or ""
        lines_added = int(getf("lines_added", 0) or 0)
        lines_removed = int(getf("lines_removed", 0) or 0)
        if not path:
            continue
        # In "whole" mode the unified_diff may be empty by design;
        # the new_content carries the full replacement. Accept either
        # so the Part lands instead of being dropped.
        if not udiff and not new_content:
            continue
        assistant_parts.append(Part(
            id=_new_part_id(),
            type="file_diff",
            path=path,
            unified_diff=udiff,
            new_content=new_content,
            status="pending",
            edit_mode=edit_mode,
            lines_added=lines_added,
            lines_removed=lines_removed,
        ))

    assistant_metadata: dict[str, Any] = {}
    if tools_called:
        assistant_metadata["tools_called"] = tools_called
    # iowarp/clio-agent#6: when streaming actually emitted chunks,
    # reuse its message_id + part_id so the deltas + final
    # message line up. Otherwise mint a fresh id (existing path).
    asst_id = streamed_assistant_msg_id or _new_message_id("asst")
    if streamed_assistant_part_id is not None and answer_text:
        # Replace the routing/text/diff parts list's text part
        # with a stub carrying the streamed part_id, so the final
        # message references the same id the deltas used.
        for i, p in enumerate(assistant_parts):
            if p.type == "text":
                assistant_parts[i] = Part(
                    id=streamed_assistant_part_id,
                    type="text",
                    text=answer_text,
                )
                break
    assistant_msg = Message(
        id=asst_id,
        session_id=sid,
        role="assistant",
        created_at=_iso_from_epoch(time.time()),
        updated_at=_iso_from_epoch(time.time()),
        parts=assistant_parts,
        tokens=Tokens(**turn_tokens),
        cost_usd=turn_cost,
        stop_reason="error" if error_info else "end_turn",
        error_info=error_info,
        metadata=assistant_metadata,
    )

    # Index file_diff parts so /diffs/apply + /diffs/reject find them.
    bucket = app.state.pending_diffs.setdefault(sid, [])
    for p in assistant_parts:
        if p.type != "file_diff":
            continue
        bucket.append({
            "path": p.path,
            "unified_diff": p.unified_diff,
            "new_content": p.new_content,
            "status": "pending",
            "part_id": p.id,
            "message_id": assistant_msg.id,
        })

    # Materialise nanoagent spawns + publish their lifecycle events.
    for spawn in nanoagents:
        get = spawn.get if isinstance(spawn, dict) else (
            lambda k, default=None, _s=spawn: getattr(_s, k, default)
        )
        agent_id = get("agent_id") or get("agent") or "nanoagent"
        spawn_input = get("input") or {}
        answer = get("answer") or ""
        subsess = app.state.sessions.create(
            workspace_id=sess.workspace_id,
            title=f"{agent_id} subagent",
            parent_session_id=sid,
        )
        sub_now = time.time()
        sub_user = Message(
            id=_new_message_id("user"),
            session_id=subsess.id,
            role="user",
            created_at=_iso_from_epoch(sub_now),
            updated_at=_iso_from_epoch(sub_now),
            parts=[Part(
                id=_new_part_id(), type="text", text=str(spawn_input),
            )],
        )
        sub_asst = Message(
            id=_new_message_id("asst"),
            session_id=subsess.id,
            role="assistant",
            created_at=_iso_from_epoch(sub_now),
            updated_at=_iso_from_epoch(sub_now),
            parts=[Part(id=_new_part_id(), type="text", text=answer)] if answer else [],
            stop_reason="end_turn",
        )
        app.state.messages.setdefault(subsess.id, []).extend(
            [sub_user, sub_asst]
        )
        app.state.sessions.update(
            subsess.id, message_count=2, status="idle"
        )
        bus.publish(Event(
            type="subagent.started",
            session_id=sid,
            payload={
                "parent_session_id": sid,
                "child_session_id": subsess.id,
                "agent_id": agent_id,
                "spawned_by_message_id": assistant_msg.id,
            },
        ))
        bus.publish(Event(
            type="subagent.completed",
            session_id=sid,
            payload={
                "parent_session_id": sid,
                "child_session_id": subsess.id,
                "agent_id": agent_id,
                "duration_ms": float(get("duration_ms", 0.0) or 0.0),
                "tokens": get("tokens") or {},
                "cost_usd": float(get("cost_usd", 0.0) or 0.0),
            },
        ))

    # message.created for the assistant message (empty body — parts
    # arrive via subsequent message.part.added/delta events).
    # When real streaming already fired the message.created +
    # message.part.added + N deltas (#6), skip re-issuing them so we
    # don't duplicate.
    if streamed_assistant_msg_id is None:
        bus.publish(Event(
            type="message.created", session_id=sid,
            payload=Message(
                id=assistant_msg.id,
                session_id=sid,
                role="assistant",
                created_at=assistant_msg.created_at,
                updated_at=assistant_msg.updated_at,
                parts=[],
            ).model_dump(exclude_none=True),
        ))
    # Stream text parts via message.part.added (empty) + N
    # message.part.delta + message.part.completed. When real
    # streaming already drained the chunks, just close out with
    # message.part.completed for the streamed text part.
    _CHUNK = 64
    for part in assistant_parts:
        if part.type == "text" and part.text:
            if part.id == streamed_assistant_part_id:
                # Real streaming already pumped deltas — but those
                # carry raw LM output that includes ChatAdapter format
                # markers ([[ ## answer ## ]] etc). The final ``part.text``
                # is the parsed clean answer; ship it on the completed
                # event so the TUI can replace the buffered text.
                bus.publish(Event(
                    type="message.part.completed",
                    session_id=sid,
                    payload={
                        "message_id": assistant_msg.id,
                        "part_id": part.id,
                        "final_text": part.text,
                    },
                ))
                continue
            stub = part.model_copy(deep=True)
            stub.text = ""
            bus.publish(Event(
                type="message.part.added",
                session_id=sid,
                payload={
                    "message_id": assistant_msg.id,
                    "part": stub.model_dump(exclude_none=True),
                },
            ))
            full = part.text
            for i in range(0, len(full), _CHUNK):
                bus.publish(Event(
                    type="message.part.delta",
                    session_id=sid,
                    payload={
                        "message_id": assistant_msg.id,
                        "part_id": part.id,
                        "delta": {"text_append": full[i:i + _CHUNK]},
                    },
                ))
            bus.publish(Event(
                type="message.part.completed",
                session_id=sid,
                payload={
                    "message_id": assistant_msg.id,
                    "part_id": part.id,
                },
            ))
        else:
            bus.publish(Event(
                type="message.part.added",
                session_id=sid,
                payload={
                    "message_id": assistant_msg.id,
                    "part": part.model_dump(exclude_none=True),
                },
            ))
    # Per-tool telemetry events synthesised from the prediction's
    # tool_called trace. A real ReAct agent that instruments
    # MCPToolBridge.call_tool publishes the same wire shape live;
    # the TUI's renderer doesn't care which flavour it is.
    for idx, call in enumerate(tools_called):
        call_id = f"call_{assistant_msg.id}_{idx}"
        bus.publish(Event(
            type="tool.call.started",
            session_id=sid,
            payload={
                "message_id": assistant_msg.id,
                "call_id": call_id,
                "tool": call.get("name", ""),
                "args": call.get("args", {}),
            },
        ))
        bus.publish(Event(
            type="tool.call.completed",
            session_id=sid,
            payload={
                "message_id": assistant_msg.id,
                "call_id": call_id,
                "tool": call.get("name", ""),
                "ok": call.get("ok", True),
                "duration_ms": call.get("duration_ms", 0.0),
                "cached": call.get("cached", False),
            },
        ))
    completed_payload: dict[str, Any] = {
        "message_id": assistant_msg.id,
        "stop_reason": "error" if error_info else "end_turn",
        "tokens": dict(turn_tokens),
        "cost_usd": turn_cost,
    }
    if tools_called:
        completed_payload["metadata"] = {"tools_called": tools_called}
    bus.publish(Event(
        type="message.completed",
        session_id=sid,
        payload=completed_payload,
    ))

    # Persist + settle.
    app.state.messages.setdefault(sid, []).append(assistant_msg)
    app.state.sessions.update(
        sid,
        status="idle" if error_info is None else "error",
        message_count=sess.message_count + 2,
        add_tokens_input=turn_tokens["input"],
        add_tokens_output=turn_tokens["output"],
        add_cost_usd=turn_cost,
    )
    bus.publish(Event(
        type="session.status_changed",
        session_id=sid,
        payload={
            "session_id": sid,
            "status": "error" if error_info else "idle",
            "prev_status": "running",
        },
    ))
    # iowarp/clio-agent#20: post_message hook runs AFTER persistence
    # so user audit code sees the settled assistant + can ship to
    # external systems. Errors are swallowed (post_* contract).
    try:
        from clio_agent.runtime.hooks import fire as _fire_hook

        _fire_hook(
            "post_message", sid,
            assistant_msg.model_dump(exclude_none=True),
        )
    except Exception:  # noqa: BLE001
        pass


def _current_lm_model_id() -> str:
    """Best-effort: which model is dspy.settings.lm bound to."""
    try:
        import dspy  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return ""
    lm = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
    return getattr(lm, "model", "") if lm else ""


def _all_known_lms(app: "FastAPI") -> list[Any]:
    """Return every LM instance the running agent might call —
    ``dspy.settings.lm`` plus the agent's ``_router_lm`` and any
    expert-bound LMs. Lets the turn handler diff history across
    all of them so router + expert + chat token counts roll up."""

    lms: list[Any] = []
    try:
        import dspy  # noqa: PLC0415
        main = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
        if main is not None:
            lms.append(main)
    except Exception:  # pragma: no cover
        pass
    agent = getattr(getattr(app, "state", None), "agent", None)
    for attr in ("_router_lm", "router_lm", "_expert_lm"):
        side = getattr(agent, attr, None) if agent is not None else None
        if side is not None and side not in lms:
            lms.append(side)
    return lms


def _snapshot_lm_history_index(app: Optional["FastAPI"] = None) -> dict[int, int]:
    """Return current ``len(lm.history)`` for every known LM,
    keyed by ``id(lm)`` so the diff side can find them again
    even if the agent rebinds attributes mid-turn."""

    if app is None:
        try:
            import dspy  # noqa: PLC0415
        except Exception:  # pragma: no cover
            return {}
        lm = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
        return {id(lm): len(getattr(lm, "history", None) or [])} if lm else {}
    snapshot: dict[int, int] = {}
    for lm in _all_known_lms(app):
        history = getattr(lm, "history", None) or []
        snapshot[id(lm)] = len(history)
    return snapshot


def _usage_from_history_slice(start: Any, app: Optional["FastAPI"] = None) -> dict[str, Any]:
    """Sum usage from each known LM's ``history[start:]`` — every
    call this turn made across router + experts + chat. Accepts
    either a ``dict[id(lm) -> int]`` snapshot (preferred) or a
    legacy single int for backwards compat with single-LM callers.
    """

    try:
        import dspy  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return {}
    if app is not None:
        lms = _all_known_lms(app)
    else:
        lm = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
        lms = [lm] if lm else []
    if not lms:
        return {}
    if isinstance(start, int):
        # Legacy single-int callers — apply to main LM only.
        snap = {id(lms[0]): start}
    else:
        snap = start
    input_tok = output_tok = cache_read = cache_write = 0
    raw_cost = 0.0
    last_model = ""
    for lm in lms:
        start_idx = snap.get(id(lm), 0)
        history = getattr(lm, "history", None) or []
        for entry in history[start_idx:]:
            if not isinstance(entry, dict):
                continue
            usage = entry.get("usage") or {}
            if not isinstance(usage, dict):
                continue
            input_tok += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            output_tok += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            cache_read += int(usage.get("cache_read_input_tokens") or 0)
            cache_write += int(usage.get("cache_creation_input_tokens") or 0)
            raw_cost += float(usage.get("cost_usd") or usage.get("total_cost") or 0.0)
            last_model = entry.get("model") or last_model
    if raw_cost == 0.0:
        raw_cost = _estimate_cost_usd(last_model, input_tok, output_tok)
    return {
        "input": input_tok,
        "output": output_tok,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cost_usd": raw_cost,
    }


def _usage_from_history_slice_legacy(start: int) -> dict[str, Any]:
    """Single-LM history diff retained for tests that don't pass
    an app. Walks dspy.settings.lm only."""

    try:
        import dspy  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return {}
    lm = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
    if lm is None:
        return {}
    history = getattr(lm, "history", None) or []
    if start >= len(history):
        return {}
    input_tok = 0
    output_tok = 0
    cache_read = 0
    cache_write = 0
    raw_cost = 0.0
    last_model = ""
    for entry in history[start:]:
        if not isinstance(entry, dict):
            continue
        usage = entry.get("usage") or {}
        if not isinstance(usage, dict):
            continue
        input_tok += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        output_tok += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        cache_read += int(usage.get("cache_read_input_tokens") or 0)
        cache_write += int(usage.get("cache_creation_input_tokens") or 0)
        raw_cost += float(usage.get("cost_usd") or usage.get("total_cost") or 0.0)
        last_model = entry.get("model") or last_model
    if raw_cost == 0.0:
        raw_cost = _estimate_cost_usd(last_model, input_tok, output_tok)
    return {
        "input": input_tok,
        "output": output_tok,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cost_usd": raw_cost,
    }


def _usage_from_tracker(tracker: Any) -> dict[str, Any]:
    """Sum usage from a per-turn ``UsageTracker`` (preferred path).

    The tracker collects per-call usage as litellm/dspy hits the LM,
    surviving the executor-thread + streaming hops that strand
    ``dspy.LM.history``. Returns ``{}`` when the tracker is absent
    or empty so the caller falls back to history scraping.
    """

    if tracker is None:
        return {}
    try:
        totals = tracker.get_total_tokens()
    except Exception:  # noqa: BLE001
        return {}
    if not totals:
        return {}
    input_tok = 0
    output_tok = 0
    cache_read = 0
    cache_write = 0
    raw_cost = 0.0
    last_model = ""
    for model, entry in totals.items():
        last_model = model
        input_tok += int(entry.get("prompt_tokens") or entry.get("input_tokens") or 0)
        output_tok += int(entry.get("completion_tokens") or entry.get("output_tokens") or 0)
        cache_read += int(entry.get("cache_read_input_tokens") or 0)
        cache_write += int(entry.get("cache_creation_input_tokens") or 0)
        raw_cost += float(entry.get("cost_usd") or entry.get("total_cost") or 0.0)
    if raw_cost == 0.0:
        raw_cost = _estimate_cost_usd(last_model, input_tok, output_tok)
    return {
        "input": input_tok,
        "output": output_tok,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cost_usd": raw_cost,
    }


def _usage_from_dspy_history() -> dict[str, Any]:
    """Reach into DSPy's currently-configured LM and pull the most
    recent call's usage block. Returns ``{}`` whenever DSPy isn't
    importable, no LM is configured, or the history is empty —
    callers default to zeros.

    Best-effort. DSPy's history shape changes between minor versions;
    we accept any dict-shaped record under ``lm.history[-1]`` whose
    ``usage`` (or ``response.usage``) carries the OpenAI-style keys
    we already use on the wire.
    """

    try:
        import dspy  # noqa: PLC0415
    except Exception:  # pragma: no cover - dspy not present
        return {}

    lm = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
    if lm is None:
        return {}
    history = getattr(lm, "history", None)
    if not history:
        return {}
    last = history[-1]
    usage = (
        last.get("usage")
        if isinstance(last, dict)
        else getattr(last, "usage", None)
    )
    if usage is None and isinstance(last, dict):
        resp = last.get("response", {}) or {}
        usage = resp.get("usage", {}) if isinstance(resp, dict) else None
    if not isinstance(usage, dict):
        return {}
    input_tok = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tok = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    raw_cost = float(usage.get("cost_usd") or usage.get("total_cost") or 0.0)
    # iowarp/clio-agent#8: Meridian (and some other proxies) don't
    # pass cost_usd through the OpenAI-compatible response shape, so
    # the upstream usage dict reports zero. Fall back to a per-token
    # price table keyed by the LM's model id when raw_cost == 0.
    if raw_cost == 0.0:
        model = ""
        if isinstance(last, dict):
            model = (
                last.get("model")
                or last.get("response", {}).get("model", "")
                or ""
            )
        else:
            model = getattr(last, "model", "") or ""
        raw_cost = _estimate_cost_usd(model, input_tok, output_tok)
    return {
        "input": input_tok,
        "output": output_tok,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "cost_usd": raw_cost,
    }


# iowarp/clio-agent#8: per-million-token prices (USD) for models we
# expect to see through our presets. Best-effort — the LM provider
# is the source of truth when it actually reports cost; this kicks
# in only when the upstream usage dict has zero. Keys match the
# substrings we look for in the reported model id (case-insensitive).
_PRICE_TABLE_PER_M: dict[str, tuple[float, float]] = {
    # (input $/M tokens, output $/M tokens) as of model-card pricing.
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4": (15.0, 75.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-3-opus": (15.0, 75.0),
    # OpenRouter free tier — by definition $0.
    ":free": (0.0, 0.0),
    # OpenAI defaults if someone wires direct.
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4o": (2.5, 10.0),
}


def _estimate_cost_usd(
    model_id: str, input_tokens: int, output_tokens: int
) -> float:
    """Best-effort cost estimate when the LM doesn't report one.

    Substring-matches the model id against ``_PRICE_TABLE_PER_M``;
    returns 0.0 when nothing matches (no false-precision number).
    """

    if not model_id:
        return 0.0
    needle = model_id.lower()
    match: Optional[tuple[float, float]] = None
    for key, prices in _PRICE_TABLE_PER_M.items():
        if key in needle:
            match = prices
            break
    if match is None:
        return 0.0
    input_price, output_price = match
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


# iowarp/clio-agent#7: tools the gate treats as destructive. Anything
# matching one of these substrings triggers a permission_requested
# event + blocks the bridge thread until the user resolves it.
_DESTRUCTIVE_TOOL_SUBSTRINGS: tuple[str, ...] = (
    "delete",
    "remove",
    "rm_",
    "drop",
    "destroy",
    "exec",
    "shell",
    "write",
)


def _is_destructive(tool_name: str) -> bool:
    n = tool_name.lower()
    return any(needle in n for needle in _DESTRUCTIVE_TOOL_SUBSTRINGS)


def _make_permission_gate(app: "FastAPI"):
    """Build a callable suitable for MCPToolBridge.permission_gate.

    Non-destructive tools fast-allow. Destructive tools register a
    permission row, publish permission.requested into the EventBus,
    block on a threading.Event with a generous timeout, and return
    "allow" / "deny" based on the user's resolution. Timeouts default
    to deny — fail-safe.
    """

    DEFAULT_TIMEOUT_S = 120.0

    def gate(name: str, args: Mapping[str, Any]) -> str:
        # iowarp/clio-agent#20: user-defined pre_tool hook can veto
        # the call by raising PermissionError. Returns ignored;
        # only the raise/no-raise distinction matters.
        try:
            from clio_agent.runtime.hooks import fire as _fire_hook

            _fire_hook("pre_tool", name, dict(args))
        except PermissionError:
            return "deny"
        if not _is_destructive(name):
            return "allow"
        # Find an active session to attach the permission to.
        # Bridge calls don't carry session context today, so fall
        # back to the most-recently-active session.
        sid = ""
        sessions_by_recency = app.state.sessions.list()
        if sessions_by_recency:
            sid = sessions_by_recency[0].id
            current = sessions_by_recency[0]
            # iowarp/clio-agent — plan_mode + architect mode reject
            # destructive tool calls without prompting. Read-only
            # contract is hard, not advisory.
            if current.mode in {"plan", "architect"}:
                row = {
                    "id": f"perm_{uuid.uuid4().hex[:12]}",
                    "session_id": sid,
                    "tool_call": {
                        "tool_name": name,
                        "input": dict(args),
                    },
                    "summary": (
                        f"destructive tool {name!r} blocked by "
                        f"session.mode={current.mode!r}"
                    ),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "status": "auto_denied",
                    "action": "deny",
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                }
                app.state.permissions[row["id"]] = row
                app.state.bus.publish(Event(
                    type="permission.resolved",
                    session_id=sid,
                    payload={
                        "permission_id": row["id"],
                        "action": "deny",
                        "session_id": sid,
                        "reason": "session_mode_readonly",
                    },
                ))
                return "deny"
        pid = f"perm_{uuid.uuid4().hex[:12]}"
        evt = threading.Event()
        row = {
            "id": pid,
            "session_id": sid,
            "tool_call": {
                "tool_name": name,
                "input": dict(args),
            },
            "summary": f"destructive tool call: {name}",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
        }
        app.state.permissions[pid] = row
        app.state.permission_events[pid] = evt
        app.state.bus.publish(Event(
            type="permission.requested",
            session_id=sid,
            payload=row,
        ))
        # Block the bridge thread until POST /v1/permissions/{pid}
        # sets the event (or we time out).
        if not evt.wait(timeout=DEFAULT_TIMEOUT_S):
            row["status"] = "timeout"
            return "deny"
        action = row.get("action", "deny")
        if action in {"allow", "allow_session", "allow_workspace"}:
            return "allow"
        return "deny"

    return gate


def _make_tool_observer(app: "FastAPI"):
    """Build a callable suitable for MCPToolBridge.tool_observer.

    Publishes tool.call.started / tool.call.completed events into
    the EventBus, attaching to the most-recently-active session
    (bridge calls don't carry session context). Also appends each
    completed call into ``app.state.tool_call_ledger[sid]`` so the
    turn handler can attach a per-turn ``tools_called`` list to the
    assistant message metadata even when the underlying expert
    didn't populate ``pred.tools_called`` itself (e.g. the
    deterministic short-circuit paths).
    """

    def observe(
        name: str,
        args: Mapping[str, Any],
        phase: Optional[str],
        error: Optional[str],
    ) -> None:
        sessions_by_recency = app.state.sessions.list()
        if not sessions_by_recency:
            return
        sid = sessions_by_recency[0].id
        if phase == "started":
            call_id = f"call_{uuid.uuid4().hex[:12]}"
            # Stash the per-thread call_id so the completion event
            # uses the same id. Threading-locals works for
            # MCPToolBridge's worker thread.
            _OBSERVER_CALL_IDS.value = call_id
            # Stamp the start time so completion can compute duration.
            _OBSERVER_CALL_T0.value = time.time()
            app.state.bus.publish(Event(
                type="tool.call.started",
                session_id=sid,
                payload={
                    "call_id": call_id,
                    "tool": name,
                    "args": dict(args),
                },
            ))
        elif phase == "completed":
            call_id = getattr(_OBSERVER_CALL_IDS, "value", "") or ""
            t0 = getattr(_OBSERVER_CALL_T0, "value", None)
            duration_ms = (time.time() - t0) * 1000 if t0 else 0.0
            ok = error is None
            payload = {
                "call_id": call_id,
                "tool": name,
                "ok": ok,
                "duration_ms": duration_ms,
                "cached": False,
                **({"error": error} if error else {}),
            }
            app.state.bus.publish(Event(
                type="tool.call.completed",
                session_id=sid,
                payload=payload,
            ))
            # Append to the per-session ledger so the turn handler
            # finds it post-forward and attaches to the assistant
            # message metadata.
            ledger = getattr(app.state, "tool_call_ledger", None)
            if ledger is not None:
                ledger.setdefault(sid, []).append({
                    "name": name,
                    "args": dict(args),
                    "ok": ok,
                    "duration_ms": duration_ms,
                    "cached": False,
                    **({"error": error} if error else {}),
                })

    return observe


_OBSERVER_CALL_T0 = threading.local()


def _agent_forward_compat(
    agent: Any,
    question: str,
    session_id: str,
    session_mode: str,
    session_edit_mode: str,
) -> Any:
    """Call agent.forward, threading session_mode + session_edit_mode
    when the agent accepts them, falling back to the legacy
    ``(question, session_id)`` signature for fakes / older builds.

    Lets us add new optional kwargs to the contract without breaking
    every test fixture that hand-rolled a minimal forward signature.
    """

    try:
        return agent.forward(
            question,
            session_id=session_id,
            session_mode=session_mode,
            session_edit_mode=session_edit_mode,
        )
    except TypeError:
        return agent.forward(question, session_id=session_id)


_OBSERVER_CALL_IDS = threading.local()


async def _try_streamed_forward(
    app: "FastAPI",
    enriched_text: str,
    sid: str,
    emit_chunk,
    session_mode: str = "chat",
    session_edit_mode: str = "diff",
) -> Optional[Any]:
    """Run the agent's forward via dspy.streamify, pumping every
    text chunk through ``emit_chunk(text)`` as it arrives. Returns
    the final dspy.Prediction on success, or None if streaming is
    unavailable / fails — caller falls back to the synchronous path.

    Falls through silently when the agent isn't a DSPy module, when
    streamify import fails, or when the wrapped call doesn't yield
    parsable text chunks. The fallback synchronous path produces
    the same wire shape (just no live deltas).
    """

    try:
        import dspy  # noqa: PLC0415
        from dspy.streaming.streamify import streamify
        from dspy.streaming.streaming_listener import StreamListener  # noqa: PLC0415
        from litellm.types.utils import ModelResponseStream  # noqa: F401
    except Exception:
        return None

    agent = app.state.agent
    if agent is None or not isinstance(agent, dspy.Module):
        return None

    # iowarp/clio-agent#6 + ChatAdapter polish: streamify wraps the
    # whole agent, so without listeners the stream pumps every LM
    # call's RAW chunks (ChatAdapter delimiters: ``[[ ## answer ## ]]``,
    # ``[[ ## reasoning ## ]]``, etc.) into the user-visible part.
    # StreamListener filters to a single signature output field —
    # we listen on ``answer`` (the chat agent's output field) so the
    # router's reasoning stream gets dropped and only the chat
    # agent's clean answer streams live. When the agent never
    # produces an ``answer`` field (e.g. expert paths return
    # ``analysis`` / ``recommendations``), the listener simply
    # stays silent and the synchronous fallback handles emit.
    # Single listener on the chat agent's "answer" field. Adding
    # listeners for other expert outputs (analysis/recommendations)
    # broke streaming entirely because find_predictor_for_stream_listeners
    # walks the program tree looking for matching Predicts and gets
    # confused by ClioAgent's complex dispatch — net result was zero
    # deltas. With one listener bound to "answer", the chat path
    # streams cleanly; expert paths fall back to the post-hoc
    # chunked emission already in the GACT layer.
    listeners = []
    try:
        listeners = [StreamListener(signature_field_name="answer")]
    except Exception:  # noqa: BLE001
        listeners = []
    # is_async_program=True keeps the agent call in the running
    # asyncio task so dspy's send_stream ContextVar propagates.
    # Without this, streamify wraps sync forward() in asyncify ->
    # runs in an executor thread -> ContextVar lost -> zero live
    # chunks. Requires the agent expose acall.
    has_acall = hasattr(agent, "acall") and callable(getattr(agent, "acall"))
    streamed = streamify(
        agent,
        async_streaming=True,
        stream_listeners=listeners,
        is_async_program=has_acall,
    )

    final_pred = None
    try:
        # StreamListener emits ``StreamResponse`` instances that
        # carry the cleaned chunk in ``.chunk``. Keep the legacy
        # ``ModelResponseStream`` / dict / str fallback for backends
        # that don't surface a typed listener payload.
        from dspy.streaming.messages import StreamResponse  # noqa: PLC0415
        # Pass session_mode + session_edit_mode if the agent's
        # forward signature accepts them (newer ClioAgent does;
        # older / fake agents fall back via TypeError catch).
        try:
            stream_iter = streamed(
                question=enriched_text,
                session_id=sid,
                session_mode=session_mode,
                session_edit_mode=session_edit_mode,
            )
        except TypeError:
            stream_iter = streamed(question=enriched_text, session_id=sid)
        async for piece in stream_iter:
            if isinstance(piece, dspy.Prediction):
                final_pred = piece
                continue
            if isinstance(piece, StreamResponse):
                if piece.chunk:
                    await emit_chunk(piece.chunk)
                continue
            text_chunk = _chunk_text(piece)
            if text_chunk:
                await emit_chunk(text_chunk)
    except Exception:
        # Streaming blew up partway through — bail to the sync
        # fallback so the user still gets an answer.
        return None
    return final_pred


def _chunk_text(piece: Any) -> str:
    """Pull a string out of whatever streamify yielded.

    Handles litellm ModelResponseStream + plain str + dict shapes.
    Returns "" when nothing's there (status-message-only chunks
    don't pollute the part body).
    """

    if isinstance(piece, str):
        return piece
    # litellm stream chunks: choices[0].delta.content
    try:
        choices = piece.choices  # type: ignore[attr-defined]
        if choices:
            delta = getattr(choices[0], "delta", None)
            if delta is not None:
                content = getattr(delta, "content", None)
                if content:
                    return str(content)
    except Exception:
        pass
    if isinstance(piece, dict):
        # OpenAI-style dict.
        try:
            return piece["choices"][0]["delta"].get("content", "") or ""
        except (KeyError, IndexError, TypeError):
            return ""
    return ""


def _apply_edit_to_disk(
    *,
    path: str,
    new_content: str,
    session: Any,
    app: "FastAPI",
) -> None:
    """Write ``new_content`` to ``path`` after enforcing the
    workspace + file_policy boundary.

    The agent's propose_edit tool put the diff together; this is
    the GACT-side commit step the user explicitly approved via
    /v1/sessions/{sid}/diffs/apply. We don't ASK for permission
    (the user already clicked apply) but we DO record an
    auto-approved permission row so /v1/permissions has a
    complete audit trail of every destructive operation.
    """

    # Audit row for the apply (auto-approved by the user's explicit
    # POST to /diffs/apply). Every destructive call lands in
    # /v1/permissions for compliance / replay.
    pid = f"perm_{uuid.uuid4().hex[:12]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    audit_row = {
        "id": pid,
        "session_id": session.id,
        "tool_call": {
            "tool_name": "fs_apply_edit_write",
            "input": {"filepath": path, "new_content_bytes": len(new_content)},
        },
        "summary": (
            f"diffs/apply: write {len(new_content)} bytes to {path}"
        ),
        "created_at": now_iso,
        "status": "auto_approved",
        "action": "allow",
        "resolved_at": now_iso,
        "reason": "user clicked /diffs/apply",
    }
    if hasattr(app.state, "permissions"):
        app.state.permissions[pid] = audit_row
    if hasattr(app.state, "bus"):
        app.state.bus.publish(Event(
            type="permission.resolved",
            session_id=session.id,
            payload={
                "permission_id": pid,
                "action": "allow",
                "session_id": session.id,
                "reason": "user_clicked_apply",
            },
        ))

    target = Path(path).resolve()
    # Workspace root scope.
    ws = app.state.workspaces.get(session.workspace_id)
    if ws is not None and ws.root_path:
        try:
            target.relative_to(Path(ws.root_path).resolve())
        except ValueError as exc:
            raise PermissionError(
                f"refused to write {target} outside workspace root "
                f"{ws.root_path}"
            ) from exc
    # Mode gate — plan + architect can't apply.
    if session.mode in {"plan", "architect"}:
        raise PermissionError(
            f"refused to write under session.mode={session.mode!r}"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(new_content, encoding="utf-8")


def _enrich_with_context_files(
    app: "FastAPI", sid: str, user_text: str
) -> str:
    """Prepend a "Context:" section to the user's text for every
    file attached to the session via /v1/sessions/{sid}/context/files.

    Behaviour by mode:
      - read / pin: read up to ``_CTX_MAX_BYTES`` from disk + inline.
      - edit: include path + size hint only (the agent fetches via
        a tool when it needs the body).

    Files outside the workspace's ``root_path`` are skipped silently
    (file_policy invariant). Files larger than the cap are inlined
    truncated with a marker.

    Returns the original ``user_text`` unchanged when no files are
    attached or all are filtered out — caller stays interface-clean.
    """

    files = (app.state.context_files.get(sid, {}) or {}).values()
    if not files:
        return user_text

    blocks: list[str] = []
    for row in files:
        path_str = row.get("path") or ""
        if not path_str:
            continue
        mode = row.get("mode") or "read"
        try:
            p = Path(path_str).resolve()
        except (OSError, ValueError):
            continue
        # iowarp/clio-agent#5: do NOT silently skip files outside the
        # workspace root — the user explicitly attached this file via
        # POST /v1/sessions/{sid}/context/files, so they know what
        # they're doing. The destructive-write gates (workspace root
        # in _apply_edit_to_disk, plus mode=plan/architect) still
        # protect against unintended writes.
        if not p.exists() or not p.is_file():
            continue
        size = p.stat().st_size
        header = f"### Context file: {path_str} (mode={mode}, {size} bytes)"
        if mode == "edit":
            blocks.append(header)
            continue
        # Scientific binary files (parquet/hdf5) don't decode as
        # useful text — dumping raw bytes leaves the LM blind. Run
        # the bundled inspection tool and inline the structured
        # summary instead. Generic mechanism: an extension → fn map.
        suffix = p.suffix.lower()
        binary_inspector = _BINARY_CONTEXT_INSPECTORS.get(suffix)
        if binary_inspector is not None:
            try:
                summary = binary_inspector(str(p))
                blocks.append(
                    header + "\n```\n" + summary + "\n```"
                )
                continue
            except Exception as exc:  # noqa: BLE001
                blocks.append(
                    header + f"\n(inspector failed: {exc!r})"
                )
                continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if len(data) > _CTX_MAX_BYTES:
            blocks.append(
                header + "\n```\n" +
                data[:_CTX_MAX_BYTES].decode(
                    "utf-8", errors="replace"
                ) +
                f"\n... ({len(data) - _CTX_MAX_BYTES} more bytes truncated)\n```"
            )
        else:
            blocks.append(
                header + "\n```\n" +
                data.decode("utf-8", errors="replace") +
                "\n```"
            )

    if not blocks:
        return user_text
    return (
        "## Attached files (auto-prepended from session context)\n\n"
        + "\n\n".join(blocks)
        + "\n\n## User question\n\n"
        + user_text
    )


_CTX_MAX_BYTES = 32 * 1024  # 32 KB cap per attached file


def _inspect_parquet_for_context(path: str) -> str:
    """Run analyze_schema on a Parquet file + return a one-paragraph
    summary the LM can quote when answering 'what's in this file'."""

    from clio_agent.tools.servers.parquet_server import analyze_schema
    fn = getattr(analyze_schema, "fn", analyze_schema)
    schema = fn(path)
    if "error" in schema:
        return f"Could not inspect Parquet file: {schema['error']}"
    cols = schema.get("columns", []) or []
    col_lines = [
        f"  - {c.get('name')}: {c.get('type')}, nullable={c.get('nullable')}"
        for c in cols[:24]
    ]
    body = (
        f"Parquet file with {schema.get('num_rows', '?')} rows, "
        f"{schema.get('num_columns', '?')} columns, "
        f"{schema.get('num_row_groups', '?')} row groups.\n"
        "Schema:\n" + "\n".join(col_lines)
    )
    if len(cols) > 24:
        body += f"\n  - ... {len(cols) - 24} more columns"
    return body


def _inspect_hdf5_for_context(path: str) -> str:
    """Run analyze_file + list_datasets on an HDF5 file + return a
    one-paragraph summary."""

    from clio_agent.tools.servers.hdf5_server import (
        analyze_file, list_datasets,
    )
    af = getattr(analyze_file, "fn", analyze_file)
    ld = getattr(list_datasets, "fn", list_datasets)
    overview = af(path)
    datasets = ld(path)
    if "error" in overview:
        return f"Could not inspect HDF5 file: {overview['error']}"
    rows = (datasets.get("datasets", []) if isinstance(datasets, dict) else []) or []
    ds_lines = [
        f"  - {d.get('path')}: shape={d.get('shape')} dtype={d.get('dtype')}"
        for d in rows[:24]
    ]
    body = (
        f"HDF5 file with {overview.get('total_datasets', len(rows))} datasets "
        f"in {overview.get('total_groups', 0)} groups.\n"
        "Datasets:\n" + "\n".join(ds_lines)
    )
    if len(rows) > 24:
        body += f"\n  - ... {len(rows) - 24} more datasets"
    return body


_BINARY_CONTEXT_INSPECTORS = {
    ".parquet": _inspect_parquet_for_context,
    ".pq": _inspect_parquet_for_context,
    ".h5": _inspect_hdf5_for_context,
    ".hdf5": _inspect_hdf5_for_context,
}


def _format_react_trajectory(traj: Any) -> str:
    """Render a DSPy ReAct trajectory (a list/dict of steps) as a
    human-readable trace. Returns "" when the input doesn't look
    like a trajectory.
    """

    if not traj:
        return ""
    rows: list[str] = []
    if isinstance(traj, dict):
        # ReAct stores as {step_n_thought, step_n_action, ...}
        idx = 0
        while True:
            thought = traj.get(f"step_{idx}_thought") or traj.get(
                f"thought_{idx}"
            )
            action = traj.get(f"step_{idx}_tool_name") or traj.get(
                f"action_{idx}"
            )
            if thought is None and action is None:
                break
            row = []
            if thought:
                row.append(f"thought: {thought}")
            if action:
                row.append(f"action: {action}")
            rows.append("  ".join(row))
            idx += 1
    elif isinstance(traj, list):
        for i, step in enumerate(traj):
            if isinstance(step, dict):
                rows.append(f"step {i}: {step}")
            else:
                rows.append(f"step {i}: {step!r}")
    return "\n".join(rows)


def _extract_tools_called(pred: Any) -> list[dict[str, Any]]:
    """Pull an agent prediction's tool-call trace into a wire-shaped
    list.

    The tier-2 experts expose their tool calls on
    ``pred.tools_called`` when the ReAct loop tracks them. Each
    entry is either a ``clio_agent.arc.schema.ToolCall`` (msgspec
    struct), a plain dict, or an object with attribute access —
    handle all three. Fields copied onto the wire when present:
    name, args, ok, duration_ms, cached. All optional.
    """

    raw = getattr(pred, "tools_called", None)
    if not raw:
        return []

    out: list[dict[str, Any]] = []
    for call in raw:
        row: dict[str, Any] = {}
        if isinstance(call, dict):
            def get(key: str, default: Any = None, _src: Any = call) -> Any:
                return _src.get(key, default)
        else:
            # msgspec structs + DSPy trace records — attribute access.
            def get(key: str, default: Any = None, _src: Any = call) -> Any:
                return getattr(_src, key, default)

        name = get("name") or get("tool") or ""
        if name:
            row["name"] = str(name)

        args = get("args")
        if args is None:
            args = get("arguments")
        if args is not None:
            row["args"] = args

        status = get("status")
        if status is not None:
            row["ok"] = status not in {"failure", "error", "timeout"}
        elif get("ok") is not None:
            row["ok"] = bool(get("ok"))

        duration_ms = get("duration_ms")
        if duration_ms is not None:
            row["duration_ms"] = float(duration_ms)

        cached = get("cached")
        if cached is not None:
            row["cached"] = bool(cached)

        if row:
            out.append(row)
    return out


# CLIO-BBBBBBBBBB10: mapping from CLIO expert id to its GACT v0.2
# specialization tag. Free-form (UI palette hint); picked to match
# the emulator's generic "code_editing / data_analysis /
# knowledge_retrieval / visualization" vocab the TUI already
# colour-codes.
_EXPERT_SPECIALIZATION: dict[str, str] = {
    "data": "data_analysis",
    "analysis": "data_analysis",
    "visualization": "data_visualization",
}

# CLIO-BBBBBBBBBB10: per-expert curated tool list. CLIO's Expert
# classes attach their tools at construction time (via
# MCPToolBridge.to_dspy_tools()), but we don't want to import DSPy +
# spin up tool servers just to list a catalog. The tool sets are
# stable so hardcoding the mapping here is cheap + honest; if an
# expert's tool set drifts, the test_agents_catalog test fails and
# we update both sides at once.
_EXPERT_TOOLS: dict[str, list[str]] = {
    "data": [
        "hdf5_list_datasets",
        "hdf5_analyze_dataset",
        "hdf5_check_compression",
        "hdf5_optimize_chunking",
        "hdf5_analyze_file",
    ],
    "analysis": [
        "parquet_analyze_schema",
        "parquet_query_data",
        "parquet_compute_statistics",
    ],
    "visualization": [
        "plot_histogram",
        "plot_bar_chart",
        "plot_scatter",
        "plot_summary",
    ],
}


def _builtin_agents() -> list[AgentDef]:
    """Return CLIO's built-in tier-2 experts as AgentDef rows.

    Imports are lazy inside the function because importing
    clio_agent.experts at module load time pulls in DSPy + the
    tool bridges — heavy, and we don't want it to explode scaffold
    tests if DSPy isn't available. Each expert exposes
    ``get_capabilities()`` returning ``{name, description, keywords,
    tools}``; we map those onto the GACT AgentDef shape.

    A tier-1 orchestrator row ('main') is synthesised so the TUI
    can see the full hierarchy; its tools list is empty (the
    orchestrator dispatches rather than acting itself).
    """

    from clio_agent.experts import get_expert_capabilities

    rows: list[AgentDef] = [
        AgentDef(
            id="main",
            source="builtin",
            title="Main Agent",
            description=(
                "Tier-1 orchestrator. Routes user queries to tier-2 "
                "specialists based on keyword heuristics + LM classifier."
            ),
            tier=1,
            specialization="orchestrator",
        ),
    ]

    for expert_id, caps in get_expert_capabilities().items():
        name = caps.get("name", expert_id.replace("_", " ").title())
        description = caps.get("description", "")
        keywords = list(caps.get("keywords", []))
        tools = list(_EXPERT_TOOLS.get(expert_id, []))
        rows.append(
            AgentDef(
                id=expert_id,
                source="builtin",
                title=name,
                description=description,
                tools=tools,
                tier=2,
                specialization=_EXPERT_SPECIALIZATION.get(
                    expert_id, expert_id
                ),
                keywords=keywords,
            )
        )

    return rows


def _builtin_tools() -> list[Tool]:
    """Flatten the experts' curated tool lists into a single GACT
    Tool catalog. Stable ids (same strings the experts reference),
    backend flag `builtin`. The names MAY duplicate across experts
    (e.g. read_file) — we dedupe by id so GET /v1/catalog/tools has
    one row per distinct tool."""

    seen: dict[str, Tool] = {}
    for agent in _builtin_agents():
        if agent.tier != 2:
            continue
        for tool_name in agent.tools:
            if tool_name in seen:
                continue
            seen[tool_name] = Tool(
                id=tool_name,
                source="builtin",
                name=tool_name,
                title=tool_name.replace("_", " ").title(),
            )
    return list(seen.values())

from typing import Protocol

from clio_agent.gact.events import Event, EventBus, heartbeat_payload
from clio_agent.gact.sessions import SessionStore, _default_store_path
from clio_agent.gact.types import (
    AgentDef,
    AuthInfo,
    BackendInfo,
    CacheStats,
    Capabilities,
    CapabilityFlags,
    CreateSessionRequest,
    CreateWorkspaceRequest,
    ErrorEnvelope,
    ErrorInfo,
    GlobalMemoryStats,
    HealthResponse,
    Integration,
    ListAgentsResponse,
    ListSessionsResponse,
    ListToolsResponse,
    ListWorkspacesResponse,
    LMProviderInfo,
    LMProviderPreset,
    LMProviderRequest,
    MemoryStats,
    Message,
    Metrics,
    MetricsMessages,
    MetricsSessions,
    Part,
    PostMessageRequest,
    PostMessageResponse,
    Session,
    SessionMemoryStats,
    Tokens,
    Tool,
    TransportFlags,
    UpdateSessionRequest,
    Workspace,
)
from clio_agent.gact.workspaces import (
    WorkspaceStore,
)
from clio_agent.gact.workspaces import (
    _default_store_path as _ws_default_store_path,
)


class AgentLike(Protocol):
    """Structural interface for anything the GACT POST-message path
    can drive. Lets tests inject a fake without pulling DSPy + a real
    LM; production wires the actual ``ClioAgent``.

    ``forward`` MUST return something with ``.answer`` (str) and
    ``.selected_expert`` (str). The real ``dspy.Prediction`` already
    matches this shape; FakeClioAgent in the tests does too.
    """

    def forward(self, question: str, session_id: str) -> Any:  # pragma: no cover
        ...

# Version pins. Keep in sync with the gact-tui SPEC.md version bump
# history; bump EMULATOR_VERSION-equivalent here only when the
# *module's* behaviour changes, not every spec revision.
CONTRACT_VERSION = "0.2"
GACT_BACKEND_VERSION = "0.1.0"  # version of this clio_agent.gact module


def _not_implemented(capability: str) -> ErrorEnvelope:
    """Build the v0.2 error envelope for a 501 response."""

    return ErrorEnvelope(
        error=ErrorInfo(
            error="config_error",
            message=f"capability not yet implemented: {capability}",
            details={
                "capability": capability,
                "note": (
                    "This endpoint is stubbed at CLIO-BBBBBBBBBB6; it will "
                    "be wired in a follow-on iteration. See "
                    "gact-tui/PLAN.md phase CLIO-BBBBBBBBBB for the roadmap."
                ),
            },
            recoverable=False,
        )
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hook.

    Spins the scheduler tick task (#21) at boot if a ScheduleStore
    is wired; cancels it cleanly on shutdown.
    """

    app.state.started_at = time.time()
    task: Optional[asyncio.Task] = None
    if getattr(app.state, "schedules", None) is not None:
        task = asyncio.create_task(_scheduler_tick(app))
        app.state.scheduler_task = task
    yield
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def _scheduler_tick(app: "FastAPI") -> None:
    """Once-a-minute loop: fire any due schedules.

    Each due schedule kicks the same _run_turn_in_background path
    a regular POST /messages would, so SSE subscribers see the
    automated turn unfold like any other.
    """

    while True:
        try:
            now = datetime.now(timezone.utc)
            for sch in list(app.state.schedules.due_now(now)):
                user_msg = Message(
                    id=_new_message_id("user"),
                    session_id=sch.session_id,
                    role="user",
                    created_at=_iso_from_epoch(time.time()),
                    updated_at=_iso_from_epoch(time.time()),
                    parts=[Part(
                        id=_new_part_id(),
                        type="text",
                        text=sch.question,
                    )],
                    metadata={"scheduled": True, "schedule_id": sch.id},
                )
                app.state.messages.setdefault(
                    sch.session_id, []
                ).append(user_msg)
                app.state.bus.publish(Event(
                    type="message.created",
                    session_id=sch.session_id,
                    payload=user_msg.model_dump(exclude_none=True),
                ))
                app.state.schedules.mark_fired(sch.id)
                # Fire-and-forget the turn task.
                asyncio.create_task(
                    _run_turn_in_background(
                        app, sch.session_id, sch.question, user_msg,
                    )
                )
        except Exception:  # noqa: BLE001
            pass
        # Sleep until just past the next minute boundary so we don't
        # double-fire on the same minute.
        await asyncio.sleep(60)


class ARCLike(Protocol):
    """Structural interface for the ARC reference /v1/memory/stats
    pulls from. Real ``ARCMemory`` matches it; tests pass a fake.

    ``get_cache_stats`` returns a dict with ``hits`` / ``misses`` /
    ``hit_rate`` / ``capacity`` (see ``ARCMemory.get_cache_stats``).
    """

    def get_cache_stats(self) -> dict[str, Any]:  # pragma: no cover
        ...


def build_app(
    sessions_path: Optional[Path] = None,
    agent: Optional[AgentLike] = None,
    arc: Optional[ARCLike] = None,
) -> FastAPI:
    """Construct the FastAPI app.

    Kept as a factory (not a module-level ``app = FastAPI()``) so
    tests can build fresh instances without singleton state; the
    module-level ``app`` below is for ``uvicorn
    clio_agent.gact.app:app`` invocations.

    ``sessions_path`` overrides where the session registry persists.
    ``None`` uses the production default (``~/.config/clio-agent/
    sessions.json``); tests pass ``tmp_path / "sessions.json"`` for
    isolation.

    ``agent`` is the ClioAgent-like object driving turns. Left
    ``None`` for builds that only exercise session CRUD without
    actual LM calls — endpoints needing an agent (POST messages, SSE)
    return a structured 503 until one is wired. Production main()
    constructs a real ``ClioAgent`` and passes it here.
    """

    app = FastAPI(
        title="CLIO GACT v0.2",
        version=GACT_BACKEND_VERSION,
        lifespan=_lifespan,
    )
    # Initialise state eagerly in case the caller skips the lifespan
    # context (TestClient normally runs it, but older FastAPI + some
    # test-utility paths don't).
    app.state.started_at = time.time()
    app.state.sessions = SessionStore(
        path=sessions_path if sessions_path is not None else _default_store_path()
    )
    app.state.agent = agent  # may be None; POST message checks before using
    app.state.arc = arc  # may be None; /v1/memory/stats returns zeros in that case
    # CLIO-BBBBBBBBBB13: per-session pub/sub. POST /messages
    # publishes; /v1/sessions/{sid}/events subscribers consume.
    app.state.bus = EventBus()
    # CLIO-BBBBBBBBBB14: in-memory message log keyed by session_id.
    # Populated by POST /messages, read by GET /messages. Not
    # persisted across restarts — disk-backed persistence lives in
    # the CLIO catch-up phase alongside ARC session replay.
    app.state.messages: dict[str, list[Message]] = {}
    # CLIO-BBBBBBBBBB20: cooperative cancellation flags. POST /cancel
    # adds a sid; the POST-message handler checks + clears after the
    # agent returns. Set (not dict) because the flag's presence IS
    # the signal — no payload.
    app.state.cancel_flags: set[str] = set()
    # CLIO-BBBBBBBBBB22: per-session context files. Keyed by
    # session_id, each value is an ordered dict of
    # path -> ContextFile dict.
    app.state.context_files: dict[str, dict[str, dict[str, Any]]] = {}
    # CLIO-BBBBBBBBBB21: per-session pending diffs. Keyed by
    # session_id -> list of {path, unified_diff, status,
    # part_id, message_id}. Status is "pending" until apply/reject
    # flips it.
    app.state.pending_diffs: dict[str, list[dict[str, Any]]] = {}
    # CLIO-BBBBBBBBBB23: pending permission requests. Flat dict
    # keyed by permission_id so GET /v1/permissions can filter by
    # session cheaply. Each record carries
    # {id, session_id, tool_call, summary, created_at, status,
    #  action, resolved_at}.
    app.state.permissions: dict[str, dict[str, Any]] = {}
    # iowarp/clio-agent#7: per-permission threading.Event so the
    # MCPToolBridge gate (running in a worker thread) can block on
    # the user's response without polling.
    app.state.permission_events: dict[str, "threading.Event"] = {}
    # iowarp/clio-agent#18: per-session task list (todo-style).
    # Keyed by session_id -> {task_id -> task dict}. In-memory.
    app.state.session_tasks: dict[str, dict[str, dict[str, Any]]] = {}
    # iowarp/clio-agent#3: per-session in-flight turn tasks. POST
    # /messages tracks the asyncio.Task here so /cancel can
    # hard-abort instead of waiting for the cooperative flag check.
    app.state.in_flight_turns: dict[str, asyncio.Task] = {}
    # iowarp/clio-agent#2: per-session ledger of tool calls observed
    # during the in-flight turn. The global tool_observer appends
    # here; _run_turn_in_background drains it post-forward to attach
    # tools_called metadata even when the underlying expert
    # didn't populate ``pred.tools_called`` itself.
    app.state.tool_call_ledger: dict[str, list[dict[str, Any]]] = {}

    # iowarp/clio-agent#7 + #2: install process-global hooks on the
    # MCPToolBridge so EVERY expert's tool call routes through our
    # permission gate + telemetry observer. Imported lazily so
    # build_app stays cheap when the tool layer isn't wanted.
    try:
        from clio_agent.tools.execution import (
            set_global_permission_gate,
            set_global_tool_observer,
        )

        set_global_permission_gate(_make_permission_gate(app))
        set_global_tool_observer(_make_tool_observer(app))
    except Exception:  # pragma: no cover - defensive
        pass

    # iowarp/clio-agent#20: install the user-hooks registry so
    # pre_tool / post_tool / pre_message / post_message events
    # route to ~/.config/clio-agent/hooks/<event>.py. Tests pre-
    # install their own registry; we only install a default if
    # nothing's currently wired so the test-side hook stays.
    try:
        from clio_agent.runtime.hooks import (
            HookRegistry,
            install_global_registry,
        )
        from clio_agent.runtime.hooks import (
            _registry as _current_registry,
        )

        if _current_registry is None:
            install_global_registry(HookRegistry())
    except Exception:  # pragma: no cover - defensive
        pass

    # CLIO-BBBBBBBBBB-D: live LM config — what the TUI configured
    # us with. Distinct from boot-time env because PUT /providers/lm
    # rebuilds the agent + DSPy config in-place.
    app.state.lm_config: Optional[dict[str, str]] = None
    # CLIO-BBBBBBBBBB-WS: workspaces store. Persisted alongside
    # sessions; seeds a default workspace if none exist so the TUI
    # always has something to render.
    app.state.workspaces = WorkspaceStore(
        path=(sessions_path.parent / "workspaces.json")
        if sessions_path is not None
        else _ws_default_store_path()
    )
    # iowarp/clio-agent#19: dynamic agent registry. Persists user-
    # registered Tier-2 specialists alongside sessions/workspaces;
    # built-ins always take precedence on id clash (rejected at
    # the HTTP layer).
    from clio_agent.gact.user_agents import (
        UserAgentStore,
    )
    from clio_agent.gact.user_agents import (
        _default_store_path as _ua_default,
    )
    app.state.user_agents = UserAgentStore(
        path=(sessions_path.parent / "agents.json")
        if sessions_path is not None
        else _ua_default()
    )
    # iowarp/clio-agent#21: scheduled turns store + tick task.
    from clio_agent.gact.scheduler import ScheduleStore as _SchedStore
    app.state.schedules = _SchedStore(
        path=(sessions_path.parent / "schedules.json")
        if sessions_path is not None else None
    )
    app.state.scheduler_task: Optional[asyncio.Task] = None
    # iowarp/clio-agent#22: shared session tokens.
    app.state.shared_tokens: dict[str, dict[str, Any]] = {}

    @app.get("/v1/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """SPEC §3.4 — per-subsystem status feeds the TUI's /doctor
        modal (v0.2 `integration_health`). We report on whatever is
        actually wired in this build: the API itself, the session
        store, the agent (real vs fake vs not-wired), and ARC.

        overall_status collapses the rows to the worst case:
        ready > degraded > unavailable.
        """

        uptime = int(time.time() - app.state.started_at)
        rows: list[Integration] = [
            Integration(
                name="api",
                status="ready",
                detail=f"clio-agent-gact {GACT_BACKEND_VERSION}",
            ),
            Integration(
                name="sessions",
                status="ready",
                detail=f"{len(app.state.sessions.list())} session(s) registered",
            ),
        ]

        agent = app.state.agent
        if agent is None:
            rows.append(Integration(
                name="agent",
                status="unavailable",
                detail="no ClioAgent wired; POST /messages will 503",
            ))
        else:
            # Heuristic: the production ClioAgent is a class that
            # imports DSPy under the hood and exposes it via
            # `agent.__class__.__module__`. The smoke/test fakes
            # live under 'gact_smoke_server' or '__main__'. Label
            # them so the /doctor modal is honest about what's
            # running.
            mod = type(agent).__module__
            is_fake = (
                "smoke" in mod
                or mod == "__main__"
                or "test" in mod.lower()
            )
            rows.append(Integration(
                name="agent",
                status="degraded" if is_fake else "ready",
                detail=(
                    f"{type(agent).__name__} (fake — dev harness)"
                    if is_fake
                    else f"{type(agent).__name__} wired"
                ),
            ))

        if app.state.arc is None:
            rows.append(Integration(
                name="memory",
                status="degraded",
                detail="memory layer not wired; /v1/memory/stats returns zeros",
            ))
        else:
            try:
                stats = app.state.arc.get_cache_stats()
                hr = stats.get("hit_rate", 0.0)
                rows.append(Integration(
                    name="memory",
                    status="ready",
                    detail=f"cache {int(hr * 100)}% hit rate",
                ))
            except Exception as exc:
                rows.append(Integration(
                    name="memory",
                    status="unavailable",
                    detail=f"memory cache stats raised: {exc!r}",
                ))

        # LM row drives the TUI's "configure provider on connect"
        # decision. ``configured`` mirrors what GET /v1/providers/lm
        # reports — agent present + last-known config from PUT.
        cfg = app.state.lm_config or {}
        if app.state.agent is not None and cfg:
            detail = f"{cfg.get('provider', '?')}/{cfg.get('model', '?')}"
            rows.append(Integration(
                name="lm",
                status="ready",
                detail=detail,
            ))
        elif app.state.agent is not None:
            # Agent wired by env at boot; lm_config wasn't recorded
            # but we know an LM is configured.
            rows.append(Integration(
                name="lm",
                status="ready",
                detail="configured from env at boot",
            ))
        else:
            rows.append(Integration(
                name="lm",
                status="unavailable",
                detail=(
                    "no LM configured; PUT /v1/providers/lm or set "
                    "CLIO_LM_PROVIDER and restart"
                ),
            ))

        # Worst-status wins.
        statuses = {r.status for r in rows}
        if "unavailable" in statuses:
            overall = "unavailable"
        elif "degraded" in statuses:
            overall = "degraded"
        else:
            overall = "ready"

        return HealthResponse(
            healthy=overall != "unavailable",
            uptime_s=uptime,
            overall_status=overall,
            integrations=rows,
        )

    @app.get("/v1/capabilities", response_model=Capabilities)
    async def capabilities() -> Capabilities:
        return Capabilities(
            contract_version=CONTRACT_VERSION,
            backend=BackendInfo(
                name="clio-agent-gact",
                version=GACT_BACKEND_VERSION,
                vendor="iowarp",
                homepage="https://github.com/iowarp/clio-agent",
            ),
            capabilities=CapabilityFlags(
                # v0.1 baseline — flipped on as each surface lands.
                # Honest reporting lets the TUI disable UI for
                # capabilities we don't actually provide.
                sessions=True,  # BBB8 — /v1/sessions CRUD
                workspaces=True,  # CLIO-WS — /v1/workspaces CRUD
                metrics=True,  # BBB15 — /v1/metrics returns SPEC §6.16 envelope
                session_branching=True,  # BBB26 — POST /sessions/{sid}/fork
                search_messages=True,  # BBB27 — GET /sessions/{sid}/messages/search
                cost_tracking=True,  # BBB24 — Message.tokens + Session.cost_usd rollup
                files=True,  # BBB22 — /v1/sessions/{sid}/context/files CRUD
                diffs=True,  # BBB21 — file_diff parts + /diffs/apply,reject
                permissions=True,  # BBB23 — /v1/permissions + permission.* events
                subagents=True,  # BBB25 — nanoagent subsessions + subagent.* events
                session_export=True,  # #16 — /v1/sessions/{sid}/export + import
                mcp=True,  # #13 — /v1/mcp/servers exposes the gateway namespaces
                providers=True,  # #15 — /v1/providers catalogs the LM presets
                commands=True,  # #14 — /v1/commands + dispatch
                thinking_blocks=True,  # #17 — DSPy reasoning trace as thinking Parts
                session_tasks=True,  # #18 — per-session todo CRUD
                plan_mode=True,  # session.mode=plan blocks destructive tools
                edit_modes=True,  # session.edit_mode toggles diff/whole/patch
                agent_write=True,  # #19 — POST/PUT/DELETE /v1/agents
                hooks=True,  # #20 — pre/post_tool + pre/post_message hooks
                scheduled_sessions=True,  # #21 — cron schedules
                session_sharing=True,  # #22 — share tokens
                skills_extraction=True,  # #23 — POST /v1/agents/extract
                # v0.2 additions — advertised when the scaffold
                # actually emits them. Turned on piecewise as the
                # follow-on items land.
                agent_routing=True,  # BBB10 — /v1/agents?tier= + tier-2 catalog
                memory=True,  # BBB11 — /v1/memory/stats backed by ARC
                structured_errors=True,  # always — we return the envelope for every error
                integration_health=True,  # /v1/health above carries it
                tool_telemetry=True,  # BBB18 — tool.call.started/completed events
            ),
            transports=TransportFlags(events_sse=True, events_websocket=False),
            auth=AuthInfo(schemes=["trust_socket"], current="trust_socket"),
        )

    # ---- 501 stubs for the rest of the surface ---------------------------
    # Every route in the v0.2 contract that we haven't wired yet
    # returns the structured error envelope from above. Matches the
    # shape v0.2 clients expect, while honestly reporting that the
    # backend doesn't yet implement the endpoint.

    # ---- /v1/sessions CRUD -----------------------------------------
    # CLIO-BBBBBBBBBB8 — four real handlers against app.state.sessions
    # (the SessionStore wired above). Kept as nested closures so they
    # can close over `app` cleanly without passing the store around.

    @app.post("/v1/sessions", response_model=Session)
    async def create_session(req: CreateSessionRequest) -> Session:
        wid = req.workspace_id or "ws_default"
        if app.state.workspaces.get(wid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"workspace not found: {wid}",
                        details={"workspace_id": wid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        sess = app.state.sessions.create(
            workspace_id=wid,
            title=req.title,
            metadata=req.metadata,
            mode=req.mode,
            edit_mode=req.edit_mode,
        )
        return Session(**sess.to_wire())

    @app.patch("/v1/sessions/{sid}", response_model=Session)
    async def patch_session(
        sid: str, req: UpdateSessionRequest
    ) -> Session:
        """Update mutable session fields (title + mode + edit_mode).

        Lets the TUI flip plan ↔ edit ↔ chat ↔ architect mid-
        session without recreating, and rename via the existing
        rename modal.
        """

        sess = app.state.sessions.update(
            sid,
            title=req.title,
            mode=req.mode,
            edit_mode=req.edit_mode,
            routing_mode=req.routing_mode,
        )
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        # Publish so live SSE subscribers see mode flips immediately.
        app.state.bus.publish(Event(
            type="session.updated",
            session_id=sid,
            payload=Session(**sess.to_wire()).model_dump(exclude_none=True),
        ))
        return Session(**sess.to_wire())

    @app.get("/v1/sessions", response_model=ListSessionsResponse)
    async def list_sessions(workspace_id: Optional[str] = None) -> ListSessionsResponse:
        rows = app.state.sessions.list(workspace_id=workspace_id)
        return ListSessionsResponse(
            sessions=[Session(**row.to_wire()) for row in rows]
        )

    @app.get("/v1/sessions/{sid}", response_model=Session)
    async def get_session(sid: str) -> Session:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return Session(**sess.to_wire())

    @app.delete("/v1/sessions/{sid}")
    async def delete_session(sid: str) -> JSONResponse:
        existed = app.state.sessions.delete(sid)
        if not existed:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return JSONResponse(status_code=204, content=None)

    # ---- /v1/permissions (BBB23) --------------------------------------

    @app.get("/v1/permissions")
    async def list_permissions(
        session_id: str = "", status: str = ""
    ) -> dict[str, Any]:
        """List permission requests.

        ?session_id=<sid> narrows to a session; ?status=pending
        hides resolved rows. Both are optional.
        """

        rows = list(app.state.permissions.values())
        if session_id:
            rows = [r for r in rows if r.get("session_id") == session_id]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        return {"permissions": rows}

    @app.post("/v1/permissions/{pid}")
    async def respond_permission(
        pid: str, request: Request
    ) -> JSONResponse:
        """Resolve a pending permission. Body: ``{action}`` where
        action is ``allow | deny | allow_session | allow_workspace``.
        Idempotent when the row is already resolved (returns the
        existing resolution rather than erroring).
        """

        row = app.state.permissions.get(pid)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"permission not found: {pid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        action = body.get("action") or ""
        if action not in {
            "allow", "deny", "allow_session", "allow_workspace"
        }:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=(
                            "action must be one of allow, deny, "
                            "allow_session, allow_workspace"
                        ),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if row.get("status") == "pending":
            row["status"] = "resolved"
            row["action"] = action
            row["resolved_at"] = datetime.now(timezone.utc).isoformat()
            # iowarp/clio-agent#7: wake any MCPToolBridge thread
            # waiting on this permission's event.
            evt = app.state.permission_events.pop(pid, None)
            if evt is not None:
                evt.set()
            app.state.bus.publish(Event(
                type="permission.resolved",
                session_id=row.get("session_id", ""),
                payload={
                    "permission_id": pid,
                    "action": action,
                    "session_id": row.get("session_id", ""),
                },
            ))
        return JSONResponse(status_code=204, content=None)

    # ---- /v1/sessions/{sid}/diffs/* (BBB21) ---------------------------

    def _filter_diff_paths(
        rows: list[dict[str, Any]], paths: list[str]
    ) -> list[dict[str, Any]]:
        """Narrow pending diffs to a given path allow-list. Empty
        list (or no param) means "every pending row"."""

        if not paths:
            return [r for r in rows if r["status"] == "pending"]
        allow = set(paths)
        return [r for r in rows if r["path"] in allow and r["status"] == "pending"]

    @app.post("/v1/sessions/{sid}/diffs/apply")
    async def diffs_apply(
        sid: str, request: Request
    ) -> dict[str, Any]:
        """Mark pending diffs as applied + actually write to disk
        via the fs_apply_edit_write MCP tool.

        Body: ``{paths: [...]}`` (optional). If omitted, every
        pending diff is applied. Returns ``{applied: [...],
        write_errors?: {...}}``. iowarp/clio-agent#4: writes are
        scoped to the session's workspace.root_path; failures
        per-path go into write_errors but don't block the rest.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        paths = [p for p in (body.get("paths") or []) if isinstance(p, str)]

        rows = app.state.pending_diffs.get(sid, [])
        targets = _filter_diff_paths(rows, paths)
        applied: list[str] = []
        write_errors: dict[str, str] = {}
        for r in targets:
            # iowarp/clio-agent#4: actually write to disk if the
            # row carries a `new_content` field. The
            # propose_edit-driven path always sets it; legacy/test
            # diffs that don't get the wire event but no write.
            new_content = r.get("new_content")
            if new_content is not None:
                try:
                    _apply_edit_to_disk(
                        path=r["path"],
                        new_content=new_content,
                        session=sess,
                        app=app,
                    )
                except Exception as exc:  # noqa: BLE001
                    write_errors[r["path"]] = repr(exc)
                    r["status"] = "apply_failed"
                    continue
            r["status"] = "applied"
            applied.append(r["path"])
            app.state.bus.publish(Event(
                type="file.diff.applied",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "path": r["path"],
                    "part_id": r.get("part_id", ""),
                    "message_id": r.get("message_id", ""),
                },
            ))
        out: dict[str, Any] = {"applied": applied}
        if write_errors:
            out["write_errors"] = write_errors
        return out

    @app.post("/v1/sessions/{sid}/diffs/reject")
    async def diffs_reject(
        sid: str, request: Request
    ) -> dict[str, list[str]]:
        """Mark pending diffs as rejected + publish events."""

        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        paths = [p for p in (body.get("paths") or []) if isinstance(p, str)]

        rows = app.state.pending_diffs.get(sid, [])
        targets = _filter_diff_paths(rows, paths)
        rejected: list[str] = []
        for r in targets:
            r["status"] = "rejected"
            rejected.append(r["path"])
            app.state.bus.publish(Event(
                type="file.diff.rejected",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "path": r["path"],
                    "part_id": r.get("part_id", ""),
                    "message_id": r.get("message_id", ""),
                },
            ))
        return {"rejected": rejected}

    # ---- /v1/sessions/{sid}/context/files (BBB22) ---------------------

    @app.get("/v1/sessions/{sid}/context/files")
    async def list_context_files(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        rows = list(app.state.context_files.get(sid, {}).values())
        return {"files": rows}

    @app.post("/v1/sessions/{sid}/context/files")
    async def add_context_file(sid: str, request: Request) -> dict[str, Any]:
        """Attach a file to the session's context. Body: ``{path,
        mode?, size?, last_modified?, language?}``. Existing rows
        for the same path are upserted so the TUI can swap modes
        without racing an explicit delete.
        """

        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        path = (body.get("path") or "").strip()
        if not path:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="missing required field: path",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        mode = body.get("mode") or "read"
        if mode not in {"edit", "read", "pin"}:
            mode = "read"
        row = {
            "path": path,
            "mode": mode,
            "added_at": datetime.now(timezone.utc).isoformat(),
            "last_modified": body.get("last_modified") or "",
            "size": int(body.get("size") or 0),
            "language": body.get("language") or "",
        }
        bucket = app.state.context_files.setdefault(sid, {})
        bucket[path] = row
        app.state.bus.publish(Event(
            type="context.file.added",
            session_id=sid,
            payload={"session_id": sid, "file": row},
        ))
        return row

    @app.delete("/v1/sessions/{sid}/context/files")
    async def remove_context_file(
        sid: str, request: Request
    ) -> JSONResponse:
        """Detach a file by path. 204 whether the path was attached
        — the TUI fires this optimistically on `d` in the context
        pane and doesn't want to error if the file was already
        removed."""

        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        path = (body.get("path") or "").strip()
        bucket = app.state.context_files.get(sid, {})
        removed = bucket.pop(path, None) if path else None
        if removed is not None:
            app.state.bus.publish(Event(
                type="context.file.removed",
                session_id=sid,
                payload={"session_id": sid, "path": path},
            ))
        return JSONResponse(status_code=204, content=None)

    # ---- POST /v1/sessions/{sid}/fork (BBB26) -------------------------

    @app.post("/v1/sessions/{sid}/fork")
    async def fork_session(sid: str, request: Request) -> JSONResponse:
        """Copy a session + its messages into a fresh session.

        Body (optional): ``{"at_message_id": "<id>", "title": "..."}``
        ``at_message_id`` truncates the copy at + including that
        message (so "branch from this point"). Absent → copy every
        stored message.

        The new session's ``parent_session_id`` points at the source
        so the TUI's sidebar can render the fork hierarchy (the v0.1
        Session already carries that field).
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        at = body.get("at_message_id") or ""
        title = body.get("title") or f"{sess.title} (fork)"

        src_msgs = list(app.state.messages.get(sid, []))
        if at:
            kept: list[Message] = []
            for m in src_msgs:
                kept.append(m)
                if m.id == at:
                    break
            src_msgs = kept

        new_sess = app.state.sessions.create(
            workspace_id=sess.workspace_id,
            title=title,
            parent_session_id=sid,
        )
        # Deep-copy parts so the fork's message log doesn't alias the
        # source's. Pydantic's model_copy gives us a snapshot.
        app.state.messages[new_sess.id] = [m.model_copy(deep=True) for m in src_msgs]
        app.state.sessions.update(
            new_sess.id, message_count=len(src_msgs)
        )
        return JSONResponse(
            status_code=201,
            content=Session(**new_sess.to_wire()).model_dump(exclude_none=True),
        )

    # ---- /v1/sessions/{sid}/tasks + /v1/tasks/{tid} (#18) ------------

    def _task_id() -> str:
        return f"task_{uuid.uuid4().hex[:12]}"

    @app.get("/v1/sessions/{sid}/tasks")
    async def list_session_tasks(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        rows = list(app.state.session_tasks.get(sid, {}).values())
        return {"tasks": rows}

    @app.post("/v1/sessions/{sid}/tasks")
    async def create_session_task(
        sid: str, request: Request
    ) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        title = (body.get("title") or "").strip()
        if not title:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="missing required field: title",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        status = body.get("status") or "pending"
        if status not in {"pending", "running", "completed", "failed"}:
            status = "pending"
        tid = _task_id()
        row = {
            "id": tid,
            "session_id": sid,
            "title": title,
            "status": status,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        app.state.session_tasks.setdefault(sid, {})[tid] = row
        return row

    def _find_task(tid: str) -> Optional[tuple[str, dict[str, Any]]]:
        for sid_key, rows in app.state.session_tasks.items():
            if tid in rows:
                return sid_key, rows[tid]
        return None

    @app.patch("/v1/tasks/{tid}")
    async def patch_task(tid: str, request: Request) -> dict[str, Any]:
        found = _find_task(tid)
        if found is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"task not found: {tid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        _, row = found
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if "title" in body and body["title"]:
            row["title"] = str(body["title"])
        if "status" in body and body["status"] in {
            "pending", "running", "completed", "failed"
        }:
            row["status"] = body["status"]
        row["updated_at"] = datetime.now(timezone.utc).isoformat()
        return row

    @app.delete("/v1/tasks/{tid}")
    async def delete_task(tid: str) -> JSONResponse:
        found = _find_task(tid)
        if found is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"task not found: {tid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        sid_key, _ = found
        app.state.session_tasks[sid_key].pop(tid, None)
        return JSONResponse(status_code=204, content=None)

    # ---- /v1/commands + dispatch (#14) --------------------------------

    _BACKEND_COMMANDS: list[dict[str, str]] = [
        {
            "id": "/clear",
            "title": "Clear session messages",
            "description": "Drop the in-memory log for the active session (does NOT touch ARC).",
            "source": "builtin",
        },
        {
            "id": "/cache-stats",
            "title": "ARC cache stats",
            "description": "Append the current ARC cache hit/miss counters as a system message.",
            "source": "builtin",
        },
        {
            "id": "/dump-trace",
            "title": "Dump last reasoning trace",
            "description": "Append the last assistant turn's DSPy reasoning (when available).",
            "source": "builtin",
        },
        {
            "id": "/optimize",
            "title": "Optimize active expert",
            "description": "(Stub) trigger SIMBA optimization on the active expert; reports a system message.",
            "source": "builtin",
        },
    ]

    @app.get("/v1/commands")
    async def list_commands() -> dict[str, Any]:
        """SPEC §6.13 — backend-provided slash commands."""

        return {"commands": _BACKEND_COMMANDS}

    @app.post("/v1/sessions/{sid}/commands/{cmd}")
    async def dispatch_command(sid: str, cmd: str) -> dict[str, Any]:
        """Dispatch a backend command for a session. Returns a
        system-style result the TUI can render inline as a message.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        # Accept "clear" or "/clear"; the TUI sends both shapes.
        cmd_id = cmd if cmd.startswith("/") else "/" + cmd
        known = {c["id"] for c in _BACKEND_COMMANDS}
        if cmd_id not in known:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"unknown command: {cmd_id}",
                        details={"known": sorted(known)},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        # Side effects + system message body per command.
        body_text: str
        if cmd_id == "/clear":
            app.state.messages.pop(sid, None)
            app.state.sessions.update(sid, message_count=0)
            app.state.bus.publish(Event(
                type="session.cleared",
                session_id=sid,
                payload={"session_id": sid},
            ))
            body_text = "session messages cleared"
        elif cmd_id == "/cache-stats":
            stats = {}
            if app.state.arc is not None:
                try:
                    stats = app.state.arc.get_cache_stats() or {}
                except Exception:
                    stats = {}
            body_text = (
                f"ARC cache: hits={stats.get('hits', 0)} "
                f"misses={stats.get('misses', 0)} "
                f"hit_rate={stats.get('hit_rate', 0.0):.2f} "
                f"capacity={stats.get('capacity', 0)}"
            )
        elif cmd_id == "/dump-trace":
            log = app.state.messages.get(sid, [])
            last_asst = next(
                (m for m in reversed(log) if m.role == "assistant"), None
            )
            if last_asst is None:
                body_text = "no assistant turns yet"
            else:
                trace_part = next(
                    (p for p in last_asst.parts if p.type == "thinking"),
                    None,
                )
                body_text = (
                    trace_part.text if trace_part is not None
                    else "no thinking trace on the last turn"
                )
        elif cmd_id == "/optimize":
            body_text = (
                "SIMBA optimization isn't wired yet — see "
                "iowarp/clio-agent for the optimizer roadmap"
            )
        else:  # pragma: no cover - guarded above
            body_text = f"unhandled command: {cmd_id}"

        # Materialise body_text as a real assistant message so the TUI
        # actually shows the result. Previously the body_text was only
        # in the POST response — the TUI's runCommandCmd discards that,
        # so /cache-stats, /dump-trace, /optimize, and /clear all looked
        # like they did nothing. Persist + publish so SSE redraws and
        # GET /messages reflects.
        from clio_agent.gact.types import Message, Part, Tokens  # noqa: PLC0415
        sys_msg = Message(
            id=f"msg_cmd_{uuid.uuid4().hex[:10]}",
            session_id=sid,
            role="assistant",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            parts=[Part(
                id=f"part_cmd_{uuid.uuid4().hex[:10]}",
                type="text",
                metadata={"synthetic": "command_result", "command": cmd_id},
                text=f"[{cmd_id}] {body_text}",
            )],
            tokens=Tokens(input=0, output=0, cache_read=0, cache_write=0),
            cost_usd=0.0,
            stop_reason="end_turn",
            metadata={"synthetic": "command_result", "command": cmd_id},
        )
        app.state.messages.setdefault(sid, []).append(sys_msg)
        app.state.bus.publish(Event(
            type="message.created",
            session_id=sid,
            payload=sys_msg.model_dump(exclude_none=True),
        ))

        return {
            "command": cmd_id,
            "session_id": sid,
            "result": {
                "type": "system_message",
                "text": body_text,
            },
        }

    # ---- /v1/providers (#15) ------------------------------------------

    @app.get("/v1/providers")
    async def list_providers() -> dict[str, Any]:
        """SPEC §6.12 — generic LM provider catalog. CLIO offers
        whatever DSPy/litellm can speak; we surface the same preset
        list /v1/providers/lm publishes but in the v0.1 wire shape
        (id, name, default_model, env_keys).
        """

        rows = []
        for p in _LM_PRESETS:
            rows.append({
                "id": p.id,
                "name": p.label,
                "default_model": p.suggested_model,
                "api_base": p.api_base,
                "env_keys": (
                    ["CLIO_LM_API_KEY"] if p.requires_api_key else []
                ),
                "description": p.description,
            })
        return {"providers": rows}

    # Per-provider model catalogs. Hand-curated rather than introspected
    # because most upstreams either don't expose a /models endpoint or
    # return hundreds of irrelevant entries. The TUI's Settings → Model
    # picker calls this once per provider and lists the rows verbatim.
    _PROVIDER_MODELS: dict[str, list[dict[str, str]]] = {
        "meridian": [
            {"id": "claude-haiku-4-5",  "name": "Claude Haiku 4.5",
             "description": "Fast + cheap. Default for CLIO development."},
            {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6",
             "description": "Balanced; better reasoning at moderate cost."},
            {"id": "claude-opus-4-6",   "name": "Claude Opus 4.6",
             "description": "Highest-capability Anthropic model. Slow + expensive."},
        ],
        "anthropic": [
            {"id": "claude-haiku-4-5-20251001",  "name": "Claude Haiku 4.5",
             "description": "Direct Anthropic. Fast + cheap."},
            {"id": "claude-sonnet-4-6-20251001", "name": "Claude Sonnet 4.6",
             "description": "Direct Anthropic. Balanced."},
            {"id": "claude-opus-4-6-20251001",   "name": "Claude Opus 4.6",
             "description": "Direct Anthropic. Highest capability."},
        ],
        "openai": [
            {"id": "gpt-4o-mini", "name": "GPT-4o mini",
             "description": "OpenAI's cheap fast model. Good default."},
            {"id": "gpt-4o",      "name": "GPT-4o",
             "description": "OpenAI's flagship multimodal model."},
            {"id": "gpt-4-turbo", "name": "GPT-4 Turbo",
             "description": "Higher capability, slower + pricier."},
        ],
        "openrouter": [
            {"id": "openai/gpt-oss-120b:free",
             "name": "GPT-OSS 120B (free)",
             "description": "Free tier. Heavily rate-limited."},
            {"id": "anthropic/claude-haiku-4-5",
             "name": "Claude Haiku 4.5 via OpenRouter",
             "description": "Pay-per-token via OpenRouter."},
            {"id": "anthropic/claude-sonnet-4-6",
             "name": "Claude Sonnet 4.6 via OpenRouter",
             "description": "Pay-per-token via OpenRouter."},
        ],
        "lm_studio": [
            {"id": "", "name": "(auto-discovered)",
             "description": "LM Studio reports the loaded model on /v1/models."},
        ],
        "ollama": [
            {"id": "llama3.2", "name": "Llama 3.2",
             "description": "Default Ollama model. Local."},
            {"id": "qwen2.5-coder:14b", "name": "Qwen2.5 Coder 14B",
             "description": "Better at code than llama3.2; same speed band."},
        ],
        "codex": [
            {"id": "gpt-5.4", "name": "GPT-5.4 (via Codex)",
             "description": "Codex's reasoning-tuned default."},
            {"id": "gpt-5", "name": "GPT-5 (via Codex)",
             "description": "Standard GPT-5 through the Codex app-server."},
            {"id": "gpt-4.1", "name": "GPT-4.1 (via Codex)",
             "description": "Older GPT-4.1 routed through Codex."},
        ],
    }

    @app.get("/v1/providers/{provider_id}/models")
    async def list_provider_models(provider_id: str) -> dict[str, Any]:
        """Per-provider curated model catalog. The TUI Settings →
        Model tab reads this for each provider it knows about. Returns
        an empty list (200, not 404) for unknown providers so the TUI
        can keep loading the rest of the catalog."""
        models = _PROVIDER_MODELS.get(provider_id, [])
        return {"models": models}

    # ---- /v1/mcp/servers (#13) ---------------------------------------

    @app.get("/v1/mcp/servers")
    async def list_mcp_servers() -> dict[str, Any]:
        """SPEC §6.7 — enumerate MCP servers the backend has mounted.

        Returns BOTH the bundled in-process servers (fs/hdf5/parquet)
        AND any third-party servers installed via POST /v1/mcp/servers.
        Each row carries id/name/status/transport/tools_count/tools.
        """

        rows = []

        # In-process bundled servers (fs/hdf5/parquet via gateway).
        try:
            from clio_agent.tools.gateway import list_capabilities
            caps = list_capabilities()
            per_server: dict[str, list[dict[str, str]]] = {}
            for tool in caps:
                srv = tool.get("server", "unknown")
                per_server.setdefault(srv, []).append(tool)
            for name, tools in sorted(per_server.items()):
                rows.append({
                    "id": f"mcp_{name}",
                    "name": name,
                    "status": "ready",
                    "transport": "in_process",
                    "tools_count": len(tools),
                    "tools": [t["name"] for t in tools],
                })
        except Exception as exc:  # noqa: BLE001
            rows.append({
                "id": "mcp_bundled_error",
                "name": "bundled-gateway",
                "status": "error",
                "transport": "in_process",
                "tools_count": 0,
                "tools": [],
                "error": f"gateway introspection failed: {exc!r}",
            })

        # Third-party servers installed at runtime.
        installed = getattr(app.state, "external_mcp_servers", {})
        for sid, info in sorted(installed.items()):
            rows.append({
                "id": sid,
                "name": info.get("name", sid),
                "status": info.get("status", "unknown"),
                "transport": info.get("transport", "unknown"),
                "tools_count": len(info.get("tools") or []),
                "tools": list(info.get("tools") or []),
                "spec": info.get("spec", {}),
            })
        return {"servers": rows}

    @app.post("/v1/mcp/servers", status_code=201)
    async def install_mcp_server(request: Request) -> dict[str, Any]:
        """Install + connect to a third-party MCP server.

        Body shapes:
        - stdio:  {"name": "everything", "transport": "stdio",
                   "command": "npx", "args": ["-y", "@modelcontextprotocol/server-everything"],
                   "env": {...}}
        - http:   {"name": "remote", "transport": "http",
                   "url": "https://mcp.example.com"}

        Connects via fastmcp.Client, lists the server's tools, and
        records the server in ``app.state.external_mcp_servers`` so
        subsequent /v1/mcp/servers GETs and tool dispatch can see it.

        Returns the same row shape /v1/mcp/servers does.
        """

        try:
            body = await request.json()
        except Exception:
            body = {}
        name = body.get("name") or body.get("id") or "unnamed"
        transport_kind = (body.get("transport") or "stdio").lower()

        try:
            from fastmcp import Client
            from fastmcp.client.transports import (
                StdioTransport, StreamableHttpTransport,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(error=ErrorInfo(
                    error="dependency_missing",
                    message=f"fastmcp Client unavailable: {exc!r}",
                    recoverable=False,
                )).model_dump(exclude_none=True),
            ) from exc

        if transport_kind == "stdio":
            command = body.get("command")
            args = body.get("args") or []
            env = body.get("env") or {}
            if not command:
                raise HTTPException(
                    status_code=422,
                    detail=ErrorEnvelope(error=ErrorInfo(
                        error="bad_request",
                        message="stdio transport requires 'command'",
                        recoverable=True,
                    )).model_dump(exclude_none=True),
                )
            transport = StdioTransport(command=command, args=list(args), env=dict(env) or None)
            spec = {"transport": "stdio", "command": command, "args": list(args)}
        elif transport_kind in {"http", "streamable-http"}:
            url = body.get("url")
            if not url:
                raise HTTPException(
                    status_code=422,
                    detail=ErrorEnvelope(error=ErrorInfo(
                        error="bad_request",
                        message="http transport requires 'url'",
                        recoverable=True,
                    )).model_dump(exclude_none=True),
                )
            transport = StreamableHttpTransport(url=url)
            spec = {"transport": "http", "url": url}
        else:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(error=ErrorInfo(
                    error="bad_request",
                    message=f"unknown transport: {transport_kind!r} (use stdio|http)",
                    recoverable=True,
                )).model_dump(exclude_none=True),
            )

        # Probe the server: connect, list tools, disconnect cleanly.
        # We re-create the Client per dispatch later (cheap for stdio,
        # no shared global state to worry about).
        tool_names: list[str] = []
        connect_error: Optional[str] = None
        try:
            async with Client(transport) as client:
                tools = await client.list_tools()
                tool_names = [t.name for t in tools]
        except Exception as exc:  # noqa: BLE001
            connect_error = repr(exc)

        sid = f"mcp_ext_{uuid.uuid4().hex[:10]}"
        if not hasattr(app.state, "external_mcp_servers"):
            app.state.external_mcp_servers: dict[str, dict[str, Any]] = {}
        info = {
            "id": sid,
            "name": name,
            "status": "ready" if connect_error is None else "error",
            "transport": transport_kind,
            "tools": tool_names,
            "spec": spec,
        }
        if connect_error:
            info["error"] = connect_error
        app.state.external_mcp_servers[sid] = info

        if connect_error is not None:
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(error=ErrorInfo(
                    error="upstream_unavailable",
                    message=f"MCP server probe failed: {connect_error}",
                    details={"id": sid, "spec": spec},
                    recoverable=True,
                )).model_dump(exclude_none=True),
            )
        return {
            "id": sid,
            "name": name,
            "status": "ready",
            "transport": transport_kind,
            "tools_count": len(tool_names),
            "tools": tool_names,
            "spec": spec,
        }

    @app.post("/v1/mcp/servers/{sid}/call")
    async def call_external_mcp_tool(sid: str, request: Request) -> dict[str, Any]:
        """Invoke a tool on an installed third-party MCP server.

        Body: {"tool": "<tool_name>", "args": {...}}

        Connects via fastmcp.Client using the spec recorded at
        install time, calls the tool, fires the same global
        tool_observer the agent uses (so SSE events + tools_called
        ledger entries land identically to in-process tools), and
        returns the structured result.
        """

        installed = getattr(app.state, "external_mcp_servers", {}) or {}
        info = installed.get(sid)
        if info is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(error=ErrorInfo(
                    error="not_found",
                    message=f"no installed MCP server: {sid}",
                    recoverable=True,
                )).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        tool_name = body.get("tool")
        tool_args = body.get("args") or {}
        if not tool_name:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(error=ErrorInfo(
                    error="bad_request",
                    message="missing 'tool' in request body",
                    recoverable=True,
                )).model_dump(exclude_none=True),
            )

        try:
            from fastmcp import Client
            from fastmcp.client.transports import (
                StdioTransport, StreamableHttpTransport,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(error=ErrorInfo(
                    error="dependency_missing",
                    message=f"fastmcp Client unavailable: {exc!r}",
                    recoverable=False,
                )).model_dump(exclude_none=True),
            ) from exc

        spec = info.get("spec", {})
        if spec.get("transport") == "stdio":
            transport = StdioTransport(
                command=spec["command"],
                args=spec.get("args") or [],
            )
        elif spec.get("transport") == "http":
            transport = StreamableHttpTransport(url=spec["url"])
        else:
            raise HTTPException(
                status_code=500,
                detail=ErrorEnvelope(error=ErrorInfo(
                    error="internal_error",
                    message=f"unknown stored transport: {spec!r}",
                    recoverable=False,
                )).model_dump(exclude_none=True),
            )

        # Fire tool observer manually so this call shows up in
        # tools_called + tool.call.* SSE events identically to an
        # agent-driven tool call. Same observer, no special path.
        try:
            from clio_agent.tools.execution import _GLOBAL_TOOL_OBSERVER
        except Exception:
            _GLOBAL_TOOL_OBSERVER = None
        observer_name = f"{info.get('name','ext')}.{tool_name}"
        if _GLOBAL_TOOL_OBSERVER is not None:
            try:
                _GLOBAL_TOOL_OBSERVER(observer_name, tool_args, "started", None)
            except Exception:
                pass
        try:
            async with Client(transport) as client:
                result = await client.call_tool(tool_name, tool_args)
            content = []
            for c in (getattr(result, "content", None) or []):
                content.append({
                    "type": getattr(c, "type", "text"),
                    "text": getattr(c, "text", str(c)),
                })
        except Exception as exc:  # noqa: BLE001
            if _GLOBAL_TOOL_OBSERVER is not None:
                try:
                    _GLOBAL_TOOL_OBSERVER(observer_name, tool_args, "completed", repr(exc))
                except Exception:
                    pass
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(error=ErrorInfo(
                    error="upstream_error",
                    message=f"tool call failed: {exc!r}",
                    recoverable=True,
                )).model_dump(exclude_none=True),
            ) from exc
        if _GLOBAL_TOOL_OBSERVER is not None:
            try:
                _GLOBAL_TOOL_OBSERVER(observer_name, tool_args, "completed", None)
            except Exception:
                pass
        return {
            "server_id": sid,
            "tool": tool_name,
            "args": tool_args,
            "content": content,
            "is_error": getattr(result, "isError", False),
        }

    # ---- /v1/sessions/{sid}/compact (Codex/CC parity) -----------------
    # Summarise the in-memory conversation transcript and replace it with
    # a compact synopsis to reclaim context. The TUI's /compact slash
    # command POSTs here. Today this is opportunistic: we ask the chat
    # agent to produce a one-paragraph summary and store it as a new
    # synthetic system message; the original transcript is preserved for
    # any future /resume work.

    @app.post("/v1/sessions/{sid}/compact")
    async def compact_session(sid: str, request: Request) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(error=ErrorInfo(
                    error="not_found",
                    message=f"session not found: {sid}",
                    recoverable=True,
                )).model_dump(exclude_none=True),
            )
        ledger = app.state.messages.get(sid, [])
        if not ledger:
            return {"session_id": sid, "compacted": False,
                    "reason": "session has no messages to compact"}

        # Build a transcript blob. Cap each message at 800 chars so a
        # huge tool-result payload doesn't dominate the prompt.
        # ledger entries are Pydantic Message models (see types.py); use
        # attribute access + model_dump() defensively for dict-shaped
        # entries the older code paths still produce.
        def _attr(o, name, default=None):
            if hasattr(o, name):
                return getattr(o, name)
            if isinstance(o, dict):
                return o.get(name, default)
            return default

        chunks: list[str] = []
        for m in ledger[-50:]:  # last 50 messages should be enough context
            role = (_attr(m, "role", "user") or "user").upper()
            for p in (_attr(m, "parts", []) or []):
                txt = (_attr(p, "text", "") or "")[:800]
                if txt.strip():
                    chunks.append(f"{role}: {txt}")
        transcript = "\n".join(chunks)
        if not transcript.strip():
            return {"session_id": sid, "compacted": False,
                    "reason": "transcript is empty after part filtering"}

        agent = app.state.agent
        if agent is None:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(error=ErrorInfo(
                    error="agent_unavailable",
                    message="no LM agent wired; configure one via PUT /v1/providers/lm",
                    recoverable=True,
                )).model_dump(exclude_none=True),
            )

        # Try to extract optional focus instructions from the body.
        try:
            body = await request.json()
        except Exception:
            body = {}
        focus = (body.get("focus") or "").strip()

        prompt = (
            "Summarise the following CLIO conversation transcript into a "
            "single paragraph (max 6 sentences). Capture the user's goal, "
            "any open questions, decisions made, and next steps. Drop "
            "minutiae and tool-call mechanics."
        )
        if focus:
            prompt += f"\n\nFocus the summary on: {focus}"
        prompt += f"\n\n--- transcript ---\n{transcript}\n--- end ---"

        try:
            summary = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: agent._run_chat_agent(prompt, ""),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(error=ErrorInfo(
                    error="upstream_error",
                    message=f"compact summarisation failed: {exc!r}",
                    recoverable=True,
                )).model_dump(exclude_none=True),
            ) from exc

        # Insert the summary as a new assistant message at the head of the
        # ledger (after archiving the originals to a parallel list so a
        # future /resume can recover full history). The TUI doesn't see
        # archived messages — only the compact summary + anything that
        # comes after it.
        archive = app.state.__dict__.setdefault("session_archives", {})
        archive.setdefault(sid, []).append({
            "compacted_at": time.time(),
            "messages": list(ledger),
        })
        from clio_agent.gact.types import Message, Part, Tokens  # noqa: PLC0415
        compact_message = Message(
            id=f"msg_compact_{uuid.uuid4().hex[:10]}",
            session_id=sid,
            role="assistant",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            parts=[Part(
                id=f"part_compact_{uuid.uuid4().hex[:10]}",
                type="text",
                metadata={"synthetic": "compact_summary"},
                text="[compact summary]\n" + (summary or "").strip(),
            )],
            tokens=Tokens(input=0, output=0, cache_read=0, cache_write=0),
            cost_usd=0.0,
            stop_reason="end_turn",
            metadata={"synthetic": "compact_summary"},
        )
        app.state.messages[sid] = [compact_message]

        # Publish so any open SSE stream redraws.
        app.state.bus.publish(Event(
            type="session.compacted",
            session_id=sid,
            payload={
                "archived_count": len(ledger),
                "summary_chars": len((summary or "")),
            },
        ))
        return {
            "session_id": sid,
            "compacted": True,
            "archived_count": len(ledger),
            "summary": summary,
        }

    @app.delete("/v1/mcp/servers/{sid}", status_code=204)
    async def uninstall_mcp_server(sid: str) -> None:
        """Drop a third-party MCP server registration. Bundled
        in-process servers (mcp_fs/mcp_hdf5/mcp_parquet) cannot be
        removed at runtime — return 404 for those."""

        installed = getattr(app.state, "external_mcp_servers", {}) or {}
        if sid not in installed:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(error=ErrorInfo(
                    error="not_found",
                    message=f"no externally-installed MCP server: {sid}",
                    recoverable=True,
                )).model_dump(exclude_none=True),
            )
        installed.pop(sid, None)
        return None

    # ---- /v1/mcp/servers/{sid}/(tools|resources|prompts) ----------------
    # Detail enumeration for the TUI MCP browser. Bundled servers are
    # introspected via the in-process gateway; external servers via a
    # short-lived fastmcp.Client connection (same transport spec used at
    # install time).

    def _bundled_server_tools(short_name: str) -> list[dict[str, Any]]:
        """Return tools for a bundled in-process server, shaped for the
        TUI's catalog detail rows (id/name/description)."""
        try:
            from clio_agent.tools.gateway import list_capabilities
            caps = list_capabilities()
        except Exception:
            return []
        out = []
        for tool in caps:
            if tool.get("server") != short_name:
                continue
            out.append({
                "id": tool.get("name", ""),
                "name": tool.get("name", ""),
                "description": tool.get("description") or "",
            })
        return out

    async def _external_mcp_inventory(
        sid: str, kind: str
    ) -> list[dict[str, Any]]:
        """Fetch tools|resources|prompts from a third-party MCP server."""
        installed = getattr(app.state, "external_mcp_servers", {}) or {}
        info = installed.get(sid)
        if info is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(error=ErrorInfo(
                    error="not_found",
                    message=f"no installed MCP server: {sid}",
                    recoverable=True,
                )).model_dump(exclude_none=True),
            )
        try:
            from fastmcp import Client
            from fastmcp.client.transports import (
                StdioTransport, StreamableHttpTransport,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(error=ErrorInfo(
                    error="dependency_missing",
                    message=f"fastmcp Client unavailable: {exc!r}",
                    recoverable=False,
                )).model_dump(exclude_none=True),
            ) from exc
        spec = info.get("spec", {})
        if spec.get("transport") == "stdio":
            transport = StdioTransport(
                command=spec["command"],
                args=spec.get("args") or [],
            )
        elif spec.get("transport") == "http":
            transport = StreamableHttpTransport(url=spec["url"])
        else:
            return []
        rows: list[dict[str, Any]] = []
        try:
            async with Client(transport) as client:
                if kind == "tools":
                    items = await client.list_tools()
                    for t in items:
                        rows.append({
                            "id": t.name,
                            "name": t.name,
                            "description": getattr(t, "description", "") or "",
                        })
                elif kind == "resources":
                    items = await client.list_resources()
                    for r in items:
                        uri = str(getattr(r, "uri", ""))
                        rows.append({
                            "id": uri or getattr(r, "name", ""),
                            "name": getattr(r, "name", "") or uri,
                            "description": getattr(r, "description", "") or "",
                        })
                elif kind == "prompts":
                    items = await client.list_prompts()
                    for p in items:
                        rows.append({
                            "id": p.name,
                            "name": p.name,
                            "description": getattr(p, "description", "") or "",
                        })
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(error=ErrorInfo(
                    error="upstream_error",
                    message=f"MCP {kind} listing failed: {exc!r}",
                    recoverable=True,
                )).model_dump(exclude_none=True),
            ) from exc
        return rows

    @app.get("/v1/mcp/servers/{sid}/tools")
    async def get_mcp_tools(sid: str) -> dict[str, Any]:
        """List tools for an MCP server. Bundled servers report what the
        in-process gateway has registered; third-party servers connect
        via fastmcp.Client and call tools/list."""
        if sid.startswith("mcp_") and sid not in (
            getattr(app.state, "external_mcp_servers", {}) or {}
        ):
            return {"tools": _bundled_server_tools(sid[len("mcp_"):])}
        return {"tools": await _external_mcp_inventory(sid, "tools")}

    @app.get("/v1/mcp/servers/{sid}/resources")
    async def get_mcp_resources(sid: str) -> dict[str, Any]:
        """List resources for an MCP server. Bundled servers don't
        expose resources today (return empty); external servers query
        resources/list via fastmcp.Client."""
        if sid.startswith("mcp_") and sid not in (
            getattr(app.state, "external_mcp_servers", {}) or {}
        ):
            return {"resources": []}
        return {"resources": await _external_mcp_inventory(sid, "resources")}

    @app.get("/v1/mcp/servers/{sid}/prompts")
    async def get_mcp_prompts(sid: str) -> dict[str, Any]:
        """List prompts for an MCP server. Bundled servers don't expose
        prompts today (return empty); external servers query
        prompts/list via fastmcp.Client."""
        if sid.startswith("mcp_") and sid not in (
            getattr(app.state, "external_mcp_servers", {}) or {}
        ):
            return {"prompts": []}
        return {"prompts": await _external_mcp_inventory(sid, "prompts")}

    # ---- /v1/sessions/{sid}/schedules (#21) --------------------------

    @app.get("/v1/sessions/{sid}/schedules")
    async def list_schedules(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        rows = [s.to_wire() for s in app.state.schedules.list(session_id=sid)]
        return {"schedules": rows}

    @app.post("/v1/sessions/{sid}/schedules")
    async def add_schedule(
        sid: str, request: Request
    ) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        cron = (body.get("cron") or "").strip()
        question = (body.get("question") or "").strip()
        if not cron or not question:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="missing required fields: cron + question",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        sch = app.state.schedules.add(
            session_id=sid, cron=cron, question=question
        )
        return sch.to_wire()

    @app.delete("/v1/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str) -> JSONResponse:
        existed = app.state.schedules.delete(schedule_id)
        if not existed:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"schedule not found: {schedule_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return JSONResponse(status_code=204, content=None)

    # ---- /v1/sessions/{sid}/share + /v1/shared/{token} (#22) ---------

    @app.post("/v1/sessions/{sid}/share")
    async def share_session(
        sid: str, request: Request
    ) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        ttl_s = int(body.get("ttl_s") or 0)
        token = "shr_" + uuid.uuid4().hex[:24]
        expires_at = ""
        if ttl_s > 0:
            expires_at = (
                datetime.now(timezone.utc).timestamp() + ttl_s
            )
        app.state.shared_tokens[token] = {
            "session_id": sid,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": expires_at,
        }
        return {
            "token": token,
            "session_id": sid,
            "url": f"/v1/shared/{token}",
            "expires_at": expires_at,
        }

    @app.get("/v1/shared/{token}")
    async def get_shared(token: str) -> dict[str, Any]:
        row = app.state.shared_tokens.get(token)
        if row is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"share token not found: {token}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        # Expiry check.
        expires_at = row.get("expires_at") or 0
        if expires_at and (
            datetime.now(timezone.utc).timestamp() > float(expires_at)
        ):
            app.state.shared_tokens.pop(token, None)
            raise HTTPException(
                status_code=410,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="share token expired",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        sid = row["session_id"]
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=(
                            f"underlying session {sid} no longer exists"
                        ),
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        msgs = app.state.messages.get(sid, [])
        return {
            "session": Session(**sess.to_wire()).model_dump(exclude_none=True),
            "messages": [m.model_dump(exclude_none=True) for m in msgs],
            "shared_at": row.get("created_at"),
        }

    # ---- /v1/agents/extract (#23) -------------------------------------

    @app.post("/v1/agents/extract", response_model=AgentDef, status_code=201)
    async def extract_agent(request: Request) -> AgentDef:
        """Extract a new dynamic agent from past sessions.

        Body: ``{session_ids: [..], agent_id: ".."}``. Walks the
        message logs of the listed sessions, harvests the most-
        common tool names called, and registers a user agent
        whose tools list reflects that pattern. Real DSPy SIMBA
        compilation is deferred — this is the heuristic baseline.
        """

        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        sids = [s for s in (body.get("session_ids") or []) if isinstance(s, str)]
        new_id = (body.get("agent_id") or "").strip()
        if not sids or not new_id:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="required: session_ids[] + agent_id",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if new_id in {"main", "data", "analysis", "visualization"}:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message=(
                            f"agent id {new_id!r} is built-in; "
                            "pick a different one"
                        ),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        # Walk the message logs.
        from collections import Counter
        tool_counts: Counter[str] = Counter()
        sample_questions: list[str] = []
        for sid in sids:
            for m in app.state.messages.get(sid, []):
                if m.role == "user":
                    text = next(
                        (p.text for p in m.parts if p.type == "text" and p.text),
                        "",
                    )
                    if text:
                        sample_questions.append(text)
                if m.role == "assistant":
                    md = m.metadata or {}
                    for call in md.get("tools_called", []) or []:
                        name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
                        if name:
                            tool_counts[name] += 1
        top_tools = [t for t, _ in tool_counts.most_common(5)]
        keywords = sorted({
            w.strip(".,").lower()
            for q in sample_questions[:5]
            for w in q.split()
            if len(w) >= 4
        })[:8]
        payload = {
            "id": new_id,
            "title": f"Extracted from {len(sids)} session(s)",
            "description": (
                f"Auto-extracted agent from {len(sids)} session log(s). "
                f"Common tools: {', '.join(top_tools) if top_tools else '(none)'}"
            ),
            "tier": 2,
            "specialization": "extracted",
            "keywords": keywords,
            "tools": top_tools,
        }
        agent = app.state.user_agents.upsert(payload)
        return AgentDef(**agent.to_wire())

    # ---- /v1/sessions/{sid}/export + /v1/sessions/import (#16) -------

    @app.get("/v1/sessions/{sid}/export")
    async def export_session(sid: str) -> dict[str, Any]:
        """SPEC §6.x — dump a session + its messages as a single
        portable JSON blob. Useful for sharing analyses, archiving,
        replay. Round-trips through POST /v1/sessions/import.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        msgs = app.state.messages.get(sid, [])
        ws = app.state.workspaces.get(sess.workspace_id)
        return {
            "version": "1",
            "session": Session(**sess.to_wire()).model_dump(exclude_none=True),
            "workspace": (
                Workspace(**ws.to_wire()).model_dump(exclude_none=True)
                if ws else None
            ),
            "messages": [m.model_dump(exclude_none=True) for m in msgs],
        }

    @app.post("/v1/sessions/import", response_model=Session)
    async def import_session(blob: dict[str, Any]) -> Session:
        """Restore a session from an export blob. Creates a fresh
        session in ws_default (or the workspace named in the blob
        if it exists locally) and re-plays the messages as already-
        settled rows. Returns the new Session row.
        """

        sess_data = blob.get("session", {})
        title = sess_data.get("title") or "imported"
        wid = "ws_default"
        if blob.get("workspace") and app.state.workspaces.get(
            blob["workspace"].get("id", "")
        ):
            wid = blob["workspace"]["id"]
        new_sess = app.state.sessions.create(
            workspace_id=wid,
            title=title,
            metadata=sess_data.get("metadata") or {},
        )
        msg_rows: list[Message] = []
        for m in blob.get("messages", []):
            try:
                msg = Message(**{**m, "session_id": new_sess.id})
                msg_rows.append(msg)
            except Exception:
                continue
        app.state.messages[new_sess.id] = msg_rows
        cost_total = sum(
            float(m.get("cost_usd", 0.0) or 0.0)
            for m in blob.get("messages", [])
        )
        in_total = sum(
            int((m.get("tokens") or {}).get("input", 0) or 0)
            for m in blob.get("messages", [])
        )
        out_total = sum(
            int((m.get("tokens") or {}).get("output", 0) or 0)
            for m in blob.get("messages", [])
        )
        app.state.sessions.update(
            new_sess.id,
            message_count=len(msg_rows),
            add_tokens_input=in_total,
            add_tokens_output=out_total,
            add_cost_usd=cost_total,
        )
        refreshed = app.state.sessions.get(new_sess.id)
        return Session(**refreshed.to_wire())

    # ---- GET /v1/sessions/{sid}/messages/search (BBB27) ---------------

    @app.get("/v1/sessions/{sid}/messages/search")
    async def search_messages(sid: str, q: str = "") -> dict[str, Any]:
        """Case-insensitive substring search across stored messages.

        Returns ``{matches: [{message_id, part_id, snippet, score}]}``.
        Score is a crude recency-biased ranking: newer hits score
        higher (+0.01 per message index) so identical snippets
        surface in turn order.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        needle = q.strip().lower()
        if not needle:
            return {"matches": []}

        matches: list[dict[str, Any]] = []
        rows = app.state.messages.get(sid, [])
        for idx, m in enumerate(rows):
            for part in m.parts:
                text = (part.text or "").lower()
                i = text.find(needle)
                if i < 0:
                    continue
                # 60-char snippet window centered on the hit.
                start = max(0, i - 30)
                end = min(len(part.text), i + len(needle) + 30)
                snippet = part.text[start:end]
                if start > 0:
                    snippet = "…" + snippet
                if end < len(part.text):
                    snippet = snippet + "…"
                matches.append({
                    "message_id": m.id,
                    "part_id": part.id,
                    "snippet": snippet,
                    "score": 1.0 + (idx * 0.01),
                })
        matches.sort(key=lambda r: r["score"], reverse=True)
        return {"matches": matches}

    # ---- POST /v1/sessions/{sid}/cancel (BBB20) -----------------------

    @app.post("/v1/sessions/{sid}/cancel")
    async def cancel_session(sid: str) -> JSONResponse:
        """Cooperative cancel of an in-flight turn on this session.

        The agent's ``forward()`` checks ``agent.is_cancelled(sid)``
        periodically (or honors a threading.Event we hand it) and
        returns early with ``error_info.error == "cancelled"``. The
        endpoint itself just flips the flag + publishes a
        ``session.cancelled`` event so any live SSE subscriber sees
        the transition without waiting for the next turn boundary.

        Returns 204 whether a turn was actually running — the TUI
        fires this on Esc/Ctrl+C speculatively and doesn't want an
        error if the race finished on its own.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        # Set the cancellation flag. The POST-message handler checks
        # this after forward() returns so even agents that don't
        # cooperate produce a cancelled-looking turn envelope.
        app.state.cancel_flags.add(sid)
        # iowarp/clio-agent#3: hard-abort the in-flight turn task.
        # The task's coroutine catches asyncio.CancelledError +
        # finalises with error="cancelled" so the wire still settles
        # cleanly. Cooperative flag stays set as a belt-and-braces:
        # if cancel races a finishing turn we still report it as
        # cancelled rather than a successful answer.
        in_flight = app.state.in_flight_turns.get(sid)
        if in_flight is not None and not in_flight.done():
            in_flight.cancel()
        app.state.sessions.update(sid, status="cancelled")
        app.state.bus.publish(Event(
            type="session.status_changed",
            session_id=sid,
            payload={
                "session_id": sid,
                "status": "cancelled",
                "prev_status": sess.status,
            },
        ))
        return JSONResponse(status_code=204, content=None)

    # ---- POST /v1/sessions/{sid}/messages (BBB9) ---------------------
    # Non-streaming turn: 1 request, 1 response body containing both
    # the stored user message + the assistant's reply. Streaming
    # (SSE on /v1/sessions/{sid}/events) lands in BBB10.

    @app.post(
        "/v1/sessions/{sid}/messages", response_model=PostMessageResponse
    )
    async def post_message(
        sid: str, req: PostMessageRequest, background_tasks: BackgroundTasks
    ) -> PostMessageResponse:
        """Accept a user message and ack immediately. The agent turn
        runs in the background; clients consume progress via the SSE
        channel (message.created, message.part.delta, ..., message.completed).

        Returning early matters: real LM turns can run for minutes
        (DSPy ReAct loops × 5-15s per Claude call). Holding the POST
        connection open for the whole turn means TUI timeouts, broken
        streaming UX, and no way to surface progress to the user.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if app.state.agent is None:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="config_error",
                        message=(
                            "ClioAgent not wired into this build. Launch "
                            "`clio-agent-gact` with CLIO_LM_PROVIDER set "
                            "or pass `agent=...` to build_app()."
                        ),
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        user_text = req.extract_text()
        if not user_text:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=(
                            "request body carried no text: expected "
                            "parts[] containing a text part or legacy "
                            "top-level text field"
                        ),
                        details={"session_id": sid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        now = time.time()
        user_msg = Message(
            id=_new_message_id("user"),
            session_id=sid,
            role="user",
            created_at=_iso_from_epoch(now),
            updated_at=_iso_from_epoch(now),
            parts=[Part(id=_new_part_id(), type="text", text=user_text)],
            metadata=req.metadata,
        )

        # Persist + publish the user message synchronously so by the
        # time the ack returns, GET /messages reflects it. Then mark
        # the session running, then schedule the turn in the
        # background and return.
        app.state.messages.setdefault(sid, []).append(user_msg)
        app.state.sessions.update(sid, status="running")
        app.state.bus.publish(Event(
            type="session.status_changed",
            session_id=sid,
            payload={"session_id": sid, "status": "running", "prev_status": "idle"},
        ))
        app.state.bus.publish(Event(
            type="message.created",
            session_id=sid,
            payload=user_msg.model_dump(exclude_none=True),
        ))

        # iowarp/clio-agent#3: switched from BackgroundTasks (which
        # doesn't expose the task back) to asyncio.create_task so
        # /v1/sessions/{sid}/cancel can hard-abort mid-flight.
        # Task is registered in app.state.in_flight_turns; the cancel
        # handler calls .cancel() on it. We schedule the task on the
        # running loop AFTER queueing background_tasks (which
        # FastAPI now runs nothing in, but kept as a hook in case
        # we want a post-response side-effect later).
        task = asyncio.create_task(
            _run_turn_in_background(app, sid, user_text, user_msg)
        )
        app.state.in_flight_turns[sid] = task

        def _drop_task(_t, _sid=sid) -> None:
            cur = app.state.in_flight_turns.get(_sid)
            if cur is _t:
                app.state.in_flight_turns.pop(_sid, None)

        task.add_done_callback(_drop_task)
        # background_tasks parameter is unused but kept on the
        # signature so existing callers (and FastAPI's docs) don't
        # change shape.
        del background_tasks

        return PostMessageResponse(
            message_id=user_msg.id,
            accepted_at=user_msg.created_at,
        )


    @app.get("/v1/sessions/{sid}/messages")
    async def list_messages(sid: str) -> dict[str, Any]:
        """List messages in a session.

        Today: in-memory log populated by POST /messages; returns
        empty when the session exists but has no turns yet. The v0.1
        wire shape (no pagination header, bare array) is what every
        v0.1 backend does; v0.2 clients accept both.
        """

        sess = app.state.sessions.get(sid)
        if sess is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        # TUI (and SPEC §6.4) expect newest-first with an optional
        # cursor for older pages. We store chronologically so reverse
        # at read time.
        rows = list(reversed(app.state.messages.get(sid, [])))
        return {
            "messages": [m.model_dump(exclude_none=True) for m in rows],
            "next_cursor": None,
        }

    # ---- /v1/agents catalog (BBB10) + dynamic registry (#19) ---------

    @app.get("/v1/agents", response_model=ListAgentsResponse)
    async def list_agents(tier: Optional[int] = None) -> ListAgentsResponse:
        """SPEC §6.5 + v0.2 §4.3.1: optional ?tier=N filter.

        Combines built-in tier-1/2 experts with any user-registered
        agents (iowarp/clio-agent#19). Built-ins always come first
        so the TUI's sidebar groups consistently.
        """

        rows = _builtin_agents() + [
            AgentDef(**row.to_wire())
            for row in app.state.user_agents.list()
        ]
        if tier is not None:
            rows = [a for a in rows if a.tier == tier]
        return ListAgentsResponse(agents=rows)

    @app.post(
        "/v1/agents", response_model=AgentDef, status_code=201
    )
    async def create_agent(req: dict[str, Any]) -> AgentDef:
        """iowarp/clio-agent#19: register a new dynamic agent.

        The agent is stored as an AgentDef row + persisted to disk;
        future GET /v1/agents calls include it. Built-in id collision
        is rejected so users can't shadow CLIO's core experts.
        Source is forced to "user" regardless of what the client sent.
        """

        agent_id = req.get("id", "")
        if agent_id in {"main", "data", "analysis", "visualization"}:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message=(
                            f"agent id {agent_id!r} is reserved for a "
                            "built-in expert; pick a different id"
                        ),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if not agent_id:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message="missing required field: id",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        # Force user-source so a malicious client can't claim builtin.
        req = dict(req)
        req["source"] = "user"
        agent = app.state.user_agents.upsert(req)
        return AgentDef(**agent.to_wire())

    @app.put("/v1/agents/{agent_id}", response_model=AgentDef)
    async def update_agent(agent_id: str, req: dict[str, Any]) -> AgentDef:
        """Replace an existing user agent. Built-ins are immutable."""

        if agent_id in {"main", "data", "analysis", "visualization"}:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message=(
                            f"agent id {agent_id!r} is a built-in; "
                            "rebuild CLIO to change its definition"
                        ),
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        if app.state.user_agents.get(agent_id) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"agent not found: {agent_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        # Force the URL id to win over the body to avoid the user
        # silently renaming via PUT. Force user source.
        body = dict(req)
        body["id"] = agent_id
        body["source"] = "user"
        agent = app.state.user_agents.upsert(body)
        return AgentDef(**agent.to_wire())

    @app.delete("/v1/agents/{agent_id}")
    async def delete_agent(agent_id: str) -> JSONResponse:
        """Drop a user-registered agent. Built-ins are immutable."""

        if agent_id in {"main", "data", "analysis", "visualization"}:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message=(
                            f"agent id {agent_id!r} is a built-in and "
                            "cannot be removed"
                        ),
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        existed = app.state.user_agents.delete(agent_id)
        if not existed:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"agent not found: {agent_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return JSONResponse(status_code=204, content=None)

    @app.get("/v1/catalog/tools", response_model=ListToolsResponse)
    async def list_tools() -> ListToolsResponse:
        return ListToolsResponse(tools=_builtin_tools())

    # ---- /v1/memory/stats (BBB11) ------------------------------------
    # Returns cache counters + per-session context retention + global
    # ARC totals. When ARC isn't wired (tests, smoke-boot scenarios)
    # returns zeros per SPEC §6.19 ("zeros are a valid signal").

    @app.get(
        "/v1/memory/stats",
        response_model=MemoryStats,
        response_model_by_alias=True,
    )
    async def memory_stats(session_id: Optional[str] = None) -> MemoryStats:
        if app.state.arc is not None:
            raw = app.state.arc.get_cache_stats()
            cache = CacheStats(
                hits=int(raw.get("hits", 0)),
                misses=int(raw.get("misses", 0)),
                hit_rate=float(raw.get("hit_rate", 0.0)),
                capacity=int(raw.get("capacity", 0)),
            )
            # ARC tracks conversation + invocation counts via the
            # index sizes it reports alongside the cache. Future: if
            # the numbers start diverging from what operators expect
            # we can call dedicated getters; for now the index sizes
            # are a good-faith approximation.
            global_stats = GlobalMemoryStats(
                conversations_total=int(raw.get("conv_index_size", 0)),
                invocations_total=int(raw.get("inv_index_size", 0)),
            )
        else:
            cache = CacheStats()
            global_stats = GlobalMemoryStats()

        session_block: Optional[SessionMemoryStats] = None
        if session_id:
            sess_rec = app.state.sessions.get(session_id)
            if sess_rec is not None:
                # CLIO tracks tokens per invocation, not per
                # session; for the TUI's purposes message_count is
                # a reasonable proxy until BBB19 moves sessions into
                # ARC and per-turn tokens become available on the
                # Session record.
                session_block = SessionMemoryStats(
                    session_id=session_id,
                    messages_retained=sess_rec.message_count,
                    tokens_retained=0,
                    tokens_budget=4000,
                    profiles_attached=0,
                )
            else:
                # Unknown session: return an empty block rather than
                # a 404. The TUI's footer chip handles zero stats
                # gracefully; a 404 would spam the logs on every
                # mis-timed fetch.
                session_block = SessionMemoryStats(session_id=session_id)

        return MemoryStats(
            cache=cache,
            session=session_block,
            global_=global_stats,
        )

    # ---- /v1/sessions/{sid}/events SSE (BBB13) -----------------------

    @app.get("/v1/sessions/{sid}/events")
    async def session_events(sid: str, request: Request) -> StreamingResponse:
        """SSE feed for one session. Emits the events POST /messages
        publishes (status_changed, message.created, message.part.*,
        message.completed) plus periodic 15-s heartbeats so HTTP
        proxies don't drop the idle connection.

        Per SPEC §7.1: streams forever until the client disconnects.
        Emits ``server.connected`` immediately so clients can confirm
        the wire is healthy before any real event arrives.
        """

        if app.state.sessions.get(sid) is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"session not found: {sid}",
                        details={"session_id": sid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        async def event_stream() -> AsyncIterator[bytes]:
            # Initial server.connected event so clients can flip
            # their UI from "connecting" to "live" immediately.
            connected = Event(
                type="server.connected",
                session_id=sid,
                payload={"server_version": GACT_BACKEND_VERSION},
            )
            yield _format_sse(connected)

            try:
                last_event_id = int(
                    request.headers.get("last-event-id", "0")
                )
            except (TypeError, ValueError):
                last_event_id = 0
            sub = app.state.bus.subscribe(sid, last_event_id=last_event_id)
            heartbeat_task: Optional[asyncio.Task] = None
            try:
                # Heartbeat task — pumps a server.heartbeat event
                # into the queue every 15s. SPEC §7.1.
                async def _heartbeat() -> None:
                    while True:
                        await asyncio.sleep(15)
                        app.state.bus.publish(
                            Event(
                                type="server.heartbeat",
                                session_id=sid,
                                payload=heartbeat_payload(),
                            )
                        )

                heartbeat_task = asyncio.create_task(_heartbeat())

                async for event in sub:
                    yield _format_sse(event)
            except asyncio.CancelledError:
                # Client disconnected. Cleanup happens in `finally`.
                pass
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # nginx: don't buffer SSE
            },
        )

    # ---- /v1/metrics (BBB15) -----------------------------------------

    @app.get("/v1/metrics", response_model=Metrics)
    async def metrics() -> Metrics:
        """Aggregate runtime metrics — SPEC §6.16.

        Today: counters synthesised from the session + in-memory
        message logs. ARC-backed per-expert latency/success-rate
        rollups come in when we reshape `ARCMemory.get_metrics()`
        into this envelope (tracked in the v0.3 roadmap); for now
        the endpoint returns the wire-compatible skeleton with zero
        tokens/cost/latencies so the TUI's Metrics tab renders
        rather than falling back to a permanent "n/a".
        """

        uptime = max(0, int(time.time() - app.state.started_at))

        all_sessions = app.state.sessions.list()
        by_status: dict[str, int] = {}
        active = 0
        for s in all_sessions:
            by_status[s.status] = by_status.get(s.status, 0) + 1
            if s.status in {"running", "idle"}:
                active += 1

        message_total = 0
        role_counts: dict[str, int] = {}
        for rows in app.state.messages.values():
            message_total += len(rows)
            for m in rows:
                role_counts[m.role] = role_counts.get(m.role, 0) + 1

        # CLIO-BBBBBBBBBB24: tokens + cost rollup across every
        # session's cumulative counters.
        from clio_agent.gact.types import MetricsCost, MetricsTokens

        tokens_input = sum(s.tokens_input for s in all_sessions)
        tokens_output = sum(s.tokens_output for s in all_sessions)
        cost_total = sum(s.cost_usd for s in all_sessions)

        return Metrics(
            uptime_s=uptime,
            sessions=MetricsSessions(
                total=len(all_sessions),
                active=active,
                by_status=by_status,
            ),
            messages=MetricsMessages(
                total=message_total,
                by_role=role_counts,
            ),
            tokens=MetricsTokens(
                input_total=tokens_input,
                output_total=tokens_output,
            ),
            cost=MetricsCost(total_usd=cost_total),
        )

    # ---- /v1/workspaces (CLIO-BBBBBBBBBB-WS) -------------------------

    @app.get("/v1/workspaces", response_model=ListWorkspacesResponse)
    async def list_workspaces() -> ListWorkspacesResponse:
        """SPEC §6.1 — list workspaces."""

        rows = app.state.workspaces.list()
        return ListWorkspacesResponse(
            workspaces=[Workspace(**w.to_wire()) for w in rows]
        )

    @app.post("/v1/workspaces", response_model=Workspace, status_code=201)
    async def create_workspace(req: CreateWorkspaceRequest) -> Workspace:
        """SPEC §6.1 — create a workspace pinned to ``root_path``."""

        ws = app.state.workspaces.create(
            name=req.name,
            root_path=req.root_path,
            metadata=req.metadata,
        )
        return Workspace(**ws.to_wire())

    @app.get("/v1/workspaces/{wid}", response_model=Workspace)
    async def get_workspace(wid: str) -> Workspace:
        ws = app.state.workspaces.get(wid)
        if ws is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"workspace not found: {wid}",
                        details={"workspace_id": wid},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return Workspace(**ws.to_wire())

    @app.delete("/v1/workspaces/{wid}")
    async def delete_workspace(wid: str) -> JSONResponse:
        """Refuses to delete ws_default — every CLIO install needs
        one workspace alive so sessions have a parent."""

        if wid == "ws_default":
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message="ws_default is not deletable",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        existed = app.state.workspaces.delete(wid)
        if not existed:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="internal_error",
                        message=f"workspace not found: {wid}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return JSONResponse(status_code=204, content=None)

    # ---- /v1/providers/lm (CLIO-BBBBBBBBBB-D) ------------------------

    _LM_PRESETS: list[LMProviderPreset] = [
        LMProviderPreset(
            id="meridian",
            label="Claude Max via Meridian",
            provider="openai",
            api_base="http://127.0.0.1:3456/v1",
            suggested_model="claude-haiku-4-5-20251001",
            requires_api_key=False,
            description=(
                "Routes through a local Meridian proxy that translates "
                "your Claude Max OAuth into an OpenAI-compatible API. "
                "Cheapest, fastest, default for CLIO development."
            ),
        ),
        LMProviderPreset(
            id="anthropic",
            label="Anthropic API",
            provider="anthropic",
            api_base="https://api.anthropic.com",
            suggested_model="claude-haiku-4-5-20251001",
            requires_api_key=True,
            description="Direct Anthropic API. Requires an ANTHROPIC_API_KEY.",
        ),
        LMProviderPreset(
            id="openai",
            label="OpenAI / ChatGPT",
            provider="openai",
            api_base="https://api.openai.com/v1",
            suggested_model="gpt-4o-mini",
            requires_api_key=True,
            description=(
                "Direct OpenAI API (powers ChatGPT + Codex CLI). "
                "Requires an OPENAI_API_KEY. Defaults to gpt-4o-mini "
                "for low cost; swap in gpt-4o or gpt-4-turbo for "
                "heavier work."
            ),
        ),
        LMProviderPreset(
            id="openrouter",
            label="OpenRouter",
            provider="openai",
            api_base="https://openrouter.ai/api/v1",
            suggested_model="openai/gpt-oss-120b:free",
            requires_api_key=True,
            description=(
                "OpenAI-compatible gateway over many providers. Free "
                "tier models (suffixed :free) work without spend but "
                "are heavily rate-limited."
            ),
        ),
        LMProviderPreset(
            id="lm_studio",
            label="LM Studio (localhost)",
            provider="lm_studio",
            api_base="http://127.0.0.1:1234/v1",
            suggested_model="",
            requires_api_key=False,
            description=(
                "Locally-hosted models via LM Studio. Model name is "
                "discovered automatically when blank."
            ),
        ),
        LMProviderPreset(
            id="ollama",
            label="Ollama (localhost)",
            provider="ollama",
            api_base="http://127.0.0.1:11434/v1",
            suggested_model="llama3.2",
            requires_api_key=False,
            description="Locally-hosted models via Ollama.",
        ),
        LMProviderPreset(
            id="codex",
            label="OpenAI Codex (via bridge)",
            provider="openai",
            api_base="http://127.0.0.1:18900/v1",
            suggested_model="gpt-5.4",
            requires_api_key=False,
            description=(
                "Routes through scripts/codex_bridge.py which fronts the "
                "Codex app-server SDK with an OpenAI-compatible HTTP "
                "interface. Requires the bridge running locally (default "
                "port 18900) and the `codex` binary on PATH."
            ),
        ),
    ]

    @app.get("/v1/providers/lm", response_model=LMProviderInfo)
    async def get_lm_provider() -> LMProviderInfo:
        """Report the live LM config — what we'd report on /doctor as
        the 'lm' integration row, plus a list of presets the TUI's
        provider picker shows.

        ``configured`` is true when an agent is wired and ready to
        run; the TUI uses this to decide whether to show the config
        modal on connect.
        """

        cfg = app.state.lm_config or {}
        return LMProviderInfo(
            configured=app.state.agent is not None,
            provider=cfg.get("provider", ""),
            api_base=cfg.get("api_base", ""),
            model=cfg.get("model", ""),
            temperature=float(cfg.get("temperature", 1.0) or 1.0),
            max_tokens=int(cfg.get("max_tokens", 32000) or 32000),
            thinking_budget=int(cfg.get("thinking_budget", 0) or 0),
            presets=_LM_PRESETS,
        )

    @app.put("/v1/providers/lm", response_model=LMProviderInfo)
    async def put_lm_provider(req: LMProviderRequest) -> LMProviderInfo:
        """Reconfigure the LM in-place. Rebuilds DSPy + the
        ClioAgent so subsequent POST /messages drive the new
        provider. The old agent's state (ARC, sessions, in-flight
        messages) is preserved across the swap.
        """

        try:
            import dspy

            from clio_agent.agent import ClioAgent
            from clio_agent.config import (
                LMProviderConfig,
                create_lm,
            )

            cfg = LMProviderConfig(
                provider=req.provider,
                api_base=req.api_base,
                model=req.model,
                api_key=req.api_key or "x",
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                thinking_budget=req.thinking_budget,
            )
            # ClioAgent.__init__ reads load_config_from_env() to
            # wire its router + experts. Stamp the env before
            # construction so the fresh agent matches what we just
            # configured for DSPy — otherwise it falls back to the
            # default provider (lm_studio) and we silently configure
            # the wrong endpoint.
            os.environ["CLIO_LM_PROVIDER"] = req.provider
            os.environ["CLIO_LM_API_BASE"] = req.api_base
            os.environ["CLIO_LM_MODEL"] = req.model
            os.environ["CLIO_LM_API_KEY"] = req.api_key or "x"
            # iowarp/clio-agent — DSPy 3.x forbids dspy.configure()
            # being re-called from a different async task than the
            # first one. PUT /v1/providers/lm comes from the FastAPI
            # request task, never the boot task, so the second call
            # always blew up. Side-step the guard by mutating
            # ``settings.main_thread_config['lm']`` directly — same
            # underlying state DSPy's __getattr__ reads, no async
            # task ownership check.
            new_lm = create_lm(cfg)
            from clio_agent.config import is_local_openai_compatible_backend  # noqa: PLC0415
            use_json_fallback = not is_local_openai_compatible_backend(cfg)
            new_adapter = dspy.ChatAdapter(use_json_adapter_fallback=use_json_fallback)
            try:
                from dspy.dsp.utils.settings import main_thread_config  # noqa: PLC0415
                main_thread_config["lm"] = new_lm
                main_thread_config["adapter"] = new_adapter
            except Exception:  # pragma: no cover - dspy missing
                dspy.configure(lm=new_lm, adapter=new_adapter)
            agent = await asyncio.get_running_loop().run_in_executor(
                None, lambda: ClioAgent(verbose=False)
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="config_error",
                        message=f"failed to configure LM: {exc}",
                        details={"original_error": type(exc).__name__},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc

        # Swap the agent + ARC atomically. Old agent isn't
        # explicitly closed because we don't know what background
        # state it owns; Python's GC will clean up.
        app.state.agent = agent
        app.state.arc = agent.arc
        app.state.lm_config = {
            "provider": req.provider,
            "api_base": req.api_base,
            "model": req.model,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "thinking_budget": req.thinking_budget,
        }
        # Publish so live SSE subscribers see the swap (TUI updates
        # its model chip without polling).
        app.state.bus.publish(Event(
            type="lm.provider.changed",
            session_id="",
            payload={
                "provider": req.provider,
                "model": req.model,
                "api_base": req.api_base,
                "temperature": req.temperature,
                "max_tokens": req.max_tokens,
            },
        ))
        return LMProviderInfo(
            configured=True,
            provider=req.provider,
            api_base=req.api_base,
            model=req.model,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            thinking_budget=req.thinking_budget,
            presets=_LM_PRESETS,
        )

    # ---- 501 stubs for the still-unwired v0.2 surface ----------------

    _stub_routes: list[tuple[str, str, str]] = [
        # (method, path, capability_name_for_error)
        # /v1/tools moved out of stubs — implemented below.
    ]

    # ---- /v1/tools (unified catalog across all MCP servers) ----------
    # Aggregates bundled (in_process) + installed (third-party) MCP
    # servers into a single flat list keyed by tool name. Each row
    # carries the source server id so the TUI can group/filter.
    @app.get("/v1/tools")
    async def list_tools_unified() -> dict[str, Any]:
        """SPEC §6.5 — unified tool catalog.

        Walks every MCP server the backend has mounted (bundled fs/
        hdf5/parquet via the in-process gateway, plus any third-party
        servers installed via POST /v1/mcp/servers) and returns a
        single flat list of tools. Each tool row carries:
        - id / name: the tool name (namespaced where the gateway
          namespaces them, e.g. "fs_read_file")
        - description: from the tool's docstring or schema
        - server_id / source: which MCP server exposes it
        - input_schema: JSON Schema (when available)
        """
        rows: list[dict[str, Any]] = []
        # Bundled in-process tools.
        try:
            from clio_agent.tools.gateway import list_capabilities  # noqa: PLC0415
            for tool in list_capabilities():
                srv = tool.get("server", "")
                rows.append({
                    "id": tool.get("name", ""),
                    "name": tool.get("name", ""),
                    "description": tool.get("description") or "",
                    "server_id": f"mcp_{srv}" if srv else "",
                    "source": "mcp",
                    "input_schema": tool.get("input_schema") or {},
                })
        except Exception as exc:  # noqa: BLE001
            rows.append({
                "id": "_bundled_error",
                "name": "_bundled_error",
                "description": f"bundled gateway introspection failed: {exc!r}",
                "source": "error",
            })

        # Third-party installed servers — query each via fastmcp.Client.
        installed = getattr(app.state, "external_mcp_servers", {}) or {}
        if installed:
            try:
                from fastmcp import Client  # noqa: PLC0415
                from fastmcp.client.transports import (  # noqa: PLC0415
                    StdioTransport, StreamableHttpTransport,
                )
            except Exception:  # noqa: BLE001
                Client = None  # type: ignore
            for sid, info in sorted(installed.items()):
                spec = info.get("spec", {})
                if Client is None:
                    continue
                if spec.get("transport") == "stdio":
                    transport = StdioTransport(
                        command=spec["command"],
                        args=spec.get("args") or [],
                    )
                elif spec.get("transport") == "http":
                    transport = StreamableHttpTransport(url=spec["url"])
                else:
                    continue
                try:
                    async with Client(transport) as client:
                        tools = await client.list_tools()
                    for t in tools:
                        rows.append({
                            "id": t.name,
                            "name": t.name,
                            "description": getattr(t, "description", "") or "",
                            "server_id": sid,
                            "source": "mcp",
                            "input_schema": getattr(t, "inputSchema", None)
                                or getattr(t, "input_schema", None) or {},
                        })
                except Exception as exc:  # noqa: BLE001
                    rows.append({
                        "id": f"{sid}_error",
                        "name": f"{sid}_error",
                        "description": f"failed to list {sid} tools: {exc!r}",
                        "server_id": sid,
                        "source": "error",
                    })
        return {"tools": rows}

    def _make_stub(cap: str):
        # Use a Request param so FastAPI doesn't try to validate
        # path/query/body params against the handler signature —
        # stubs take anything and return 501.
        async def _stub(request: Request) -> JSONResponse:
            body = _not_implemented(cap).model_dump(exclude_none=True)
            return JSONResponse(status_code=501, content=body)

        return _stub

    for method, path, cap in _stub_routes:
        app.add_api_route(
            path,
            _make_stub(cap),
            methods=[method],
            include_in_schema=False,
        )

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(
        request, exc: HTTPException
    ) -> JSONResponse:
        """Wrap HTTPExceptions in the v0.2 error envelope."""

        if isinstance(exc.detail, dict) and "error" in exc.detail:
            # Already an envelope (caller built one explicitly).
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        envelope = ErrorEnvelope(
            error=ErrorInfo(
                error="internal_error",
                message=str(exc.detail) if exc.detail else "",
                recoverable=exc.status_code < 500,
            )
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope.model_dump(exclude_none=True),
        )

    return app


# Module-level app for uvicorn-style invocations:
#   uvicorn clio_agent.gact.app:app
app = build_app()


def main() -> None:
    """Console-script entry point.

    When ``CLIO_LM_PROVIDER`` is set the real ``ClioAgent`` is
    instantiated + injected so POST /messages drives a real LM.
    Otherwise the module-level ``app`` (no agent wired) runs, which
    is fine for capability introspection but 503s on /messages.
    """

    import uvicorn

    parser = argparse.ArgumentParser(
        prog="clio-agent-gact",
        description="CLIO's GACT v0.2 REST + SSE server.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8100, type=int)
    parser.add_argument(
        "--reload",
        action="store_true",
        help="auto-reload on source changes (dev only)",
    )
    parser.add_argument(
        "--no-agent",
        action="store_true",
        help=(
            "skip ClioAgent construction even when LM env is configured. "
            "Use when the real agent's boot cost (DSPy + ARC hydration) "
            "gets in the way of a capability-only smoke."
        ),
    )
    args = parser.parse_args()

    # CLIO-BBBBBBBBBB-D2: auto-wire the real ClioAgent when the env
    # gives us an LM endpoint. Falls back to the no-agent module-
    # level app on import / construction failures — a bootable GACT
    # surface is strictly better than a stack trace.
    app_to_run: FastAPI = app
    if not args.no_agent and os.environ.get("CLIO_LM_PROVIDER"):
        try:
            import dspy

            from clio_agent.agent import ClioAgent
            from clio_agent.config import create_lm, load_config_from_env

            provider_cfg = load_config_from_env()
            from clio_agent.config import is_local_openai_compatible_backend  # noqa: PLC0415
            use_json_fallback = not is_local_openai_compatible_backend(provider_cfg)
            dspy.configure(
                lm=create_lm(provider_cfg),
                adapter=dspy.ChatAdapter(use_json_adapter_fallback=use_json_fallback),
            )
            agent = ClioAgent(verbose=False)
            app_to_run = build_app(agent=agent, arc=agent.arc)
        except Exception as exc:  # noqa: BLE001
            print(
                f"[clio-agent-gact] ClioAgent init failed ({exc!r}); "
                "running with no agent wired. POST /messages will 503.",
                flush=True,
            )

    uvicorn.run(
        app_to_run,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
