"""Per-turn context enrichment + context-frame provenance helpers (#714).

This module prepares one turn's *prompt context* and records what went into it:

* **Context-file injection** -- ``_enrich_with_context_files`` prepends the bodies
  (or, for ``mode=edit``/binary files, structured summaries) of every file the
  user attached via ``POST /v1/sessions/{sid}/context/files``. ``@path`` markers
  in the user text are rewritten to display paths, read/pin files are required
  context (a resolve/stat/read failure raises a structured
  :class:`~clio_agent.gact.runtime.globals._ContextFileAccessError` via
  ``_context_file_access_error`` rather than proceeding with missing context),
  and ``_BINARY_CONTEXT_INSPECTORS`` is the generic extension->inspector hook for
  scientific formats.
* **Explicit memory search** -- ``_memory_search_request_from_message`` reads the
  opt-in request off the user message metadata and
  ``_enrich_with_requested_memory_search`` runs it and inlines the ranked hits,
  emitting ``memory.search.completed`` so the recall stays visible.
* **Context frames** -- ``_record_context_frame`` snapshots the assembled context
  (visible transcript + attached files, with token estimates) into
  ``app.state.context_frames`` and publishes ``context.frame.created``;
  ``_finalize_context_frame`` stamps the assistant message id + terminal status
  and publishes ``context.frame.completed``. ``_message_text_for_frame`` /
  ``_estimate_context_tokens`` are their leaf token-accounting helpers.
* **Approved edit commit** -- ``_apply_edit_to_disk`` is the GACT-side write step
  for a diff the user explicitly approved via ``POST /v1/sessions/{sid}/diffs/apply``;
  it enforces the workspace + mode + file-policy boundary and records an
  auto-approved permission audit row.
* **Turn provenance** -- ``_context_file_turn_provenance`` returns non-secret
  provenance for the context files attached to a turn.
* **Workspace resources** -- bounded state for ``resource_ref`` parts.

The module imports only leaves and never imports :mod:`clio_agent.gact.app` at
module top. ``app`` is passed explicitly so handlers do not close over app locals.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from clio_agent.gact import context_references
from clio_agent.gact.events import Event
from clio_agent.gact.permission_gate import (
    _policy_action_for_tool,
    _record_resolved_permission,
)
from clio_agent.gact.providers.config import _active_lm_model_ref
from clio_agent.gact.resource_enrichment import enrich_with_workspace_resources
from clio_agent.gact.runtime import bringup_timing
from clio_agent.gact.runtime.constants import _CTX_MAX_BYTES
from clio_agent.gact.runtime.globals import (
    _ContextFileAccessError,
    _new_context_frame_id,
    _session_agent_id,
)
from clio_agent.gact.runtime.memory_search import _memory_search_response
from clio_agent.gact.runtime.retention import enforce_list_bound
from clio_agent.gact.types import ErrorInfo
from clio_agent.tools.file_policy import validate_write_path
from clio_agent.tools.fs_write import write_text_with_policy

if TYPE_CHECKING:
    from fastapi import FastAPI

    from clio_agent.gact.types import Message


# Core no longer bundles in-process scientific format servers, so it ships no
# built-in binary context inspectors. Structured inspection of attached binary
# files (parquet/hdf5/...) is the job of declared MCP tools the active pack
# brings in. The extension -> inspector map stays as a generic, currently-empty
# hook so the context-file path below is unchanged.
_BINARY_CONTEXT_INSPECTORS: dict[str, Any] = {}


def _context_file_access_error(
    *,
    path: str,
    mode: str,
    operation: str,
    message: str,
    original_error: BaseException | None = None,
) -> _ContextFileAccessError:
    """Build a structured GACT error for context-file preparation failures."""

    details: dict[str, Any] = {
        "path": path,
        "mode": mode,
        "operation": operation,
        "recovery_actions": [
            "reattach_context_file",
            "remove_context_file",
            "retry",
            "exit",
        ],
    }
    if original_error is not None:
        details["original_error"] = type(original_error).__name__
        details["original_message"] = str(original_error)
    return _ContextFileAccessError(
        ErrorInfo(
            error="context_file_error",
            message=message,
            details=details,
            recoverable=True,
        )
    )


def _estimate_context_tokens(text: str) -> int:
    return max(1, len(text) // 4) if text else 0


def _message_text_for_frame(message: "Message") -> str:
    chunks: list[str] = []
    for part in getattr(message, "parts", []) or []:
        text = getattr(part, "text", "") or ""
        if text:
            chunks.append(text)
        for attr in ("path", "unified_diff", "new_content"):
            value = getattr(part, attr, "") or ""
            if value:
                chunks.append(str(value))
    return "\n".join(chunks)


def _record_context_frame(
    app: "FastAPI",
    sid: str,
    sess: Any,
    user_msg: "Message",
    *,
    user_text: str,
    enriched_text: str,
    context_error: Optional[ErrorInfo],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    visible_messages = list(app.state.messages.get(sid, []))
    items: list[dict[str, Any]] = []
    token_total = 0
    for msg in visible_messages:
        msg_text = _message_text_for_frame(msg)
        tokens = (
            int(getattr(msg.tokens, "input", 0) or 0)
            + int(getattr(msg.tokens, "output", 0) or 0)
            + int(getattr(msg.tokens, "cache_read", 0) or 0)
            + int(getattr(msg.tokens, "cache_write", 0) or 0)
        )
        if tokens <= 0:
            tokens = _estimate_context_tokens(msg_text)
        token_total += tokens
        items.append(
            {
                "kind": "message",
                "source_id": msg.id,
                "role": msg.role,
                "included": True,
                "reason": "visible_transcript",
                "tokens_estimated": tokens,
                "metadata": {
                    "synthetic": (msg.metadata or {}).get("synthetic", ""),
                    "part_count": len(msg.parts),
                },
            }
        )

    for row in (app.state.context_files.get(sid, {}) or {}).values():
        path = str(row.get("resolved_path") or row.get("path") or "")
        display_path = str(row.get("display_path") or row.get("path") or path)
        try:
            raw_size = int(row.get("size") or 0)
        except (TypeError, ValueError):
            raw_size = 0
        tokens = max(0, min(max(raw_size, 0), _CTX_MAX_BYTES) // 4)
        token_total += tokens
        items.append(
            {
                "kind": "context_file",
                "source_id": display_path,
                "path": path,
                "display_path": display_path,
                "included": context_error is None,
                "reason": "attached_context_file" if context_error is None else "context_error",
                "tokens_estimated": tokens,
                "metadata": {
                    "mode": row.get("mode", ""),
                    "source": row.get("source", ""),
                    "workspace_id": row.get("workspace_id", ""),
                    "language": row.get("language", ""),
                },
            }
        )

    reference_items = context_references.context_reference_frame_items(user_msg)
    items.extend(reference_items)
    token_total += sum(int(item.get("tokens_estimated", 0) or 0) for item in reference_items)

    enriched_delta = max(0, len(enriched_text) - len(user_text))
    agent_ref = getattr(sess, "agent", {}) or {}
    frame = {
        "id": _new_context_frame_id(),
        "session_id": sid,
        "turn_id": user_msg.id,
        "user_message_id": user_msg.id,
        "assistant_message_id": "",
        "created_at": now,
        "updated_at": now,
        "status": "context_error" if context_error is not None else "assembled",
        "model": _active_lm_model_ref(app),
        "agent": {
            "id": _session_agent_id(sess),
            "mode": agent_ref.get("mode", "") if isinstance(agent_ref, dict) else "",
            "routing_mode": getattr(sess, "routing_mode", "auto"),
            "session_mode": getattr(sess, "mode", "edit"),
            "edit_mode": getattr(sess, "edit_mode", "diff"),
        },
        "prompt": {
            "profile": (getattr(sess, "metadata", {}) or {}).get("prompt_profile", ""),
            "source": "runtime_default",
        },
        "items": items,
        "tokens_estimated": token_total,
        "metadata": {
            "retained_context_source": "visible_gact_transcript",
            "token_estimate": "message_tokens_or_chars_div_4",
            "context_file_injected_chars": enriched_delta,
            "context_error": context_error.model_dump(exclude_none=True)
            if context_error is not None
            else {},
        },
    }
    frames = app.state.context_frames.setdefault(sid, [])
    frames.append(frame)
    enforce_list_bound(app, frames, "context_frames", session_id=sid)
    # NOT on the served UI wire: the context frame ("what the agent saw" — included
    # messages, token estimates) is observability the TUI surfaces on demand, not a
    # ReAct atom it renders inline. It stays queryable via
    # GET /v1/sessions/{sid}/context/frames[/{frame_id}] (routes/diffs.py).
    return frame


def _finalize_context_frame(
    app: "FastAPI",
    sid: str,
    frame_id: str,
    assistant_message_id: str,
    status: str,
    *,
    error_info: Optional[ErrorInfo],
) -> None:
    frames = app.state.context_frames.get(sid, [])
    for frame in frames:
        if frame.get("id") != frame_id:
            continue
        frame["assistant_message_id"] = assistant_message_id
        frame["status"] = status
        frame["updated_at"] = datetime.now(timezone.utc).isoformat()
        if error_info is not None:
            frame.setdefault("metadata", {})["turn_error"] = error_info.model_dump(
                exclude_none=True
            )
        # Not on the served UI wire (see _create/_record context frame above) — the
        # finalized frame is read on demand via the context/frames endpoint.
        break


def _apply_edit_to_disk(
    *,
    path: str,
    new_content: str,
    session: Any,
    app: "FastAPI",
) -> dict[str, Any]:
    """Write ``new_content`` to ``path`` after enforcing the
    workspace + file_policy boundary.

    The agent's propose_edit tool put the diff together; this is
    the GACT-side commit step the user explicitly approved via
    /v1/sessions/{sid}/diffs/apply. We don't ASK for permission
    (the user already clicked apply) but we DO record an
    auto-approved permission row so /v1/permissions has a
    complete audit trail of every destructive operation.
    """

    target = Path(path).resolve(strict=False)
    # Workspace root scope.
    ws = app.state.workspaces.get(session.workspace_id)
    if ws is not None and ws.root_path:
        try:
            target.relative_to(Path(ws.root_path).resolve())
        except ValueError as exc:
            raise PermissionError(
                f"refused to write {target} outside workspace root {ws.root_path}"
            ) from exc
    target = validate_write_path(path, field="path")

    # Mode gate — plan/architect are read-only. P1.1 #1063: this is no longer a private
    # ``session.mode in {plan, architect}`` predicate; it rides the SAME resolver as the live
    # tool gate. Passing ``mode`` makes ``_policy_action_for_tool`` consult the built-in plan_acl
    # rows, so a write here is denied in plan/architect exactly as at the gate (ONE enforcement
    # path), while a ``<plans>/*.md`` write in plan mode is permitted by the @70 carve-out.
    permission_args = {
        "filepath": str(target),
        "new_content_bytes": len(new_content),
    }
    policy_action = _policy_action_for_tool(
        app,
        session_id=session.id,
        session=session,
        tool_name="fs_apply_edit_write",
        args=permission_args,
        mode=str(getattr(session, "mode", "") or ""),
    )
    if policy_action == "deny":
        _record_resolved_permission(
            app,
            session_id=session.id,
            tool_name="fs_apply_edit_write",
            args=permission_args,
            status="auto_denied",
            action="deny",
            summary=f"diffs/apply blocked by permission policy for {target}",
            reason="policy_deny",
        )
        raise PermissionError(
            f"refused to write {target} because a permission policy denied fs_apply_edit_write"
        )

    # Audit row for the apply (auto-approved by the user's explicit
    # POST to /diffs/apply). Every destructive call lands in
    # /v1/permissions for compliance / replay.
    _record_resolved_permission(
        app,
        session_id=session.id,
        tool_name="fs_apply_edit_write",
        args=permission_args,
        status="auto_approved",
        action="allow",
        summary=f"diffs/apply: write {len(new_content)} bytes to {target}",
        reason="user_clicked_apply",
    )

    write_result = write_text_with_policy(str(target), new_content)
    # Seam (b), #966 S1: mint an artifact.created for the user-approved harness write
    # (mechanism harness, hashed-at-use from the sha256 the writer returned in-hand).
    # The owner module is fully guarded — a mint must never break the approved write.
    from clio_agent.gact.artifacts.minting import mint_harness_write  # noqa: PLC0415

    mint_harness_write(app, session, str(target), write_result)
    return write_result


def _enrich_with_context_files(app: "FastAPI", sid: str, user_text: str) -> str:
    """Prepend a "Context:" section to the user's text for every
    file attached to the session via /v1/sessions/{sid}/context/files.

    Behaviour by mode:
      - read / pin: read up to ``_CTX_MAX_BYTES`` from disk + inline.
      - edit: include path + size hint only (the agent fetches via
        a tool when it needs the body).

    Read/pin files are requested context. If they cannot be resolved,
    found, inspected, or read, the turn raises a structured error
    instead of proceeding with missing context. Edit entries can
    point at files that do not exist yet, so they stay visible as
    edit targets without requiring a body.

    Returns the original ``user_text`` unchanged when no files are
    attached.
    """

    files = (app.state.context_files.get(sid, {}) or {}).values()
    if not files:
        return user_text

    blocks: list[str] = []
    for row in files:
        path_str = row.get("resolved_path") or row.get("path") or ""
        display_path = row.get("display_path") or row.get("path") or path_str
        if not path_str:
            continue
        for marker in {
            f"@{display_path}",
            f"@{row.get('path') or ''}",
            f"@{Path(display_path).name}",
        }:
            if marker != "@":
                user_text = user_text.replace(marker, display_path)
        mode = row.get("mode") or "read"
        try:
            p = Path(path_str).resolve()
        except (OSError, ValueError) as exc:
            raise _context_file_access_error(
                path=path_str,
                mode=mode,
                operation="resolve",
                message=f"Could not resolve attached context file: {path_str}",
                original_error=exc,
            ) from exc
        # iowarp/clio-agent#5: do NOT silently skip files outside the
        # workspace root — the user explicitly attached this file via
        # POST /v1/sessions/{sid}/context/files, so they know what
        # they're doing. The destructive-write gates (workspace root
        # in _apply_edit_to_disk, plus mode=plan/architect) still
        # protect against unintended writes.
        if mode == "edit" and not p.exists():
            blocks.append(
                f"### Context file: {display_path} (mode=edit, target does not exist yet)"
            )
            continue
        if not p.exists():
            raise _context_file_access_error(
                path=path_str,
                mode=mode,
                operation="exists",
                message=f"Attached context file no longer exists: {path_str}",
            )
        if not p.is_file():
            raise _context_file_access_error(
                path=path_str,
                mode=mode,
                operation="is_file",
                message=f"Attached context path is not a file: {path_str}",
            )
        try:
            size = p.stat().st_size
        except OSError as exc:
            raise _context_file_access_error(
                path=path_str,
                mode=mode,
                operation="stat",
                message=f"Could not stat attached context file: {path_str}",
                original_error=exc,
            ) from exc
        header = f"### Context file: {display_path} (mode={mode}, {size} bytes)"
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
                blocks.append(header + "\n```\n" + summary + "\n```")
                continue
            except Exception as exc:  # noqa: BLE001
                raise _context_file_access_error(
                    path=path_str,
                    mode=mode,
                    operation="inspect",
                    message=(f"Could not inspect attached binary context file: {path_str}"),
                    original_error=exc,
                ) from exc
        try:
            data = p.read_bytes()
        except OSError as exc:
            raise _context_file_access_error(
                path=path_str,
                mode=mode,
                operation="read",
                message=f"Could not read attached context file: {path_str}",
                original_error=exc,
            ) from exc
        if len(data) > _CTX_MAX_BYTES:
            blocks.append(
                header
                + "\n```\n"
                + data[:_CTX_MAX_BYTES].decode("utf-8", errors="replace")
                + f"\n... ({len(data) - _CTX_MAX_BYTES} more bytes truncated)\n```"
            )
        else:
            blocks.append(header + "\n```\n" + data.decode("utf-8", errors="replace") + "\n```")

    if not blocks:
        return user_text
    return (
        "## Attached files (auto-prepended from session context)\n\n"
        + "\n\n".join(blocks)
        + "\n\n## User question\n\n"
        + user_text
    )


def _memory_search_request_from_message(
    message: "Message", user_text: str
) -> dict[str, Any] | None:
    raw = message.metadata.get("memory_search") if isinstance(message.metadata, Mapping) else None
    if raw is None and isinstance(message.metadata, Mapping):
        if not message.metadata.get("include_cross_session_memory"):
            return None
        raw = {
            "enabled": True,
            "query": message.metadata.get("memory_search_query") or user_text,
            "include_cross_session": True,
            "reason": message.metadata.get("memory_search_reason") or "",
        }
    if not isinstance(raw, Mapping):
        return None
    if raw.get("enabled") is False:
        return None
    return dict(raw)


def _enrich_with_requested_memory_search(
    app: "FastAPI",
    sid: str,
    user_text: str,
    user_msg: "Message",
) -> tuple[str, dict[str, Any]]:
    """Prepend explicitly requested memory-search hits to one turn.

    This is intentionally opt-in through user message metadata. It gives the
    orchestrator/TUI a tool-like way to make cross-session recall visible to the
    model without weakening the default per-session context boundary.
    """

    req = _memory_search_request_from_message(user_msg, user_text)
    if req is None:
        return user_text, {}

    query = str(req.get("query") or user_text).strip()
    include_cross_session = bool(req.get("include_cross_session", False))
    workspace_id = str(req.get("workspace_id") or "").strip()
    reason = str(req.get("reason") or "").strip()
    try:
        limit = int(req.get("limit", 5) or 5)
    except (TypeError, ValueError):
        limit = 5
    response = _memory_search_response(
        app,
        query=query,
        session_id=sid,
        workspace_id=workspace_id,
        include_cross_session=include_cross_session,
        limit=limit,
        exclude_message_id=user_msg.id,
    )
    metadata = {
        "query": response.query,
        "include_cross_session": response.include_cross_session,
        "searched_sessions": response.searched_sessions,
        "hit_count": len(response.hits),
        "reason": reason,
        "scope": response.metadata.get("scope", ""),
        "hits": [
            {
                "session_id": hit.session_id,
                "session_title": hit.session_title,
                "message_id": hit.message_id,
                "part_id": hit.part_id,
                "role": hit.role,
                "match_terms": hit.match_terms,
                "score": hit.score,
                "cross_session": bool(hit.metadata.get("cross_session", False)),
            }
            for hit in response.hits
        ],
    }
    app.state.bus.publish(
        Event(
            type="memory.search.completed",
            session_id=sid,
            payload=metadata,
        )
    )
    if not response.hits:
        return user_text, metadata

    blocks = []
    for idx, hit in enumerate(response.hits, start=1):
        cross = "cross-session" if hit.metadata.get("cross_session") else "current-session"
        title = hit.session_title or hit.session_id
        blocks.append(
            f"### Memory hit {idx}: {title} ({cross})\n"
            f"- session_id: {hit.session_id}\n"
            f"- message_id: {hit.message_id}\n"
            f"- role: {hit.role}\n"
            f"- matched_terms: {', '.join(hit.match_terms)}\n"
            f"```\n{hit.text}\n```"
        )
    return (
        "## Explicit Memory Search Results\n\n"
        + f"Query: {response.query}\n"
        + f"Reason: {reason or 'not provided'}\n"
        + f"Scope: {metadata['scope']}\n\n"
        + "\n\n".join(blocks)
        + "\n\n## User question\n\n"
        + user_text
    ), metadata


def enrich_turn_context(
    app: "FastAPI", sid: str, user_text: str, user_msg: "Message"
) -> tuple[str, dict[str, Any]]:
    """#1215 S5: both enrichment mechanisms as ONE timed "enrichment" phase.

    Pure timed combinator -- delegates unchanged to the two real functions
    above (no logic moves); the turn loop's single call site replaces its
    former two separate calls with this one.
    """

    with bringup_timing.timer_for_session(app, sid).phase("enrichment"):
        text = _enrich_with_context_files(app, sid, user_text)
        text = enrich_with_workspace_resources(app, sid, text, user_msg)
        text = context_references.enrich_with_context_references(app, sid, text, user_msg)
        return _enrich_with_requested_memory_search(app, sid, text, user_msg)


# Clio-owned marker for the server-composed observe-later notification block
# (#948 S6). The block is SERVER grounding prepended to the model's turn input —
# never user text and never model output — so it carries this constant header (the
# #881 marker discipline). The constant is DEFINED here and USED by the composer
# (:func:`inject_pending_agent_task_notifications`) to head every injected block,
# and is exported for the injection tests. NOTE: no presentation-model splitter
# keys off this marker on this lineage TODAY — the split machinery that would
# register it and keep the block out of the user-text lane is future work (see
# CHANGELOG); the marker is a stable, greppable header now and the registration
# hook when that machinery lands. It is NOT a machine-enforced trust boundary yet.
PENDING_TASK_NOTIFICATION_MARKER = (
    "## Background agent-task results (spawned in an earlier turn — you decide what to do)"
)
# Injection is BOUNDED: at most this many task blocks per turn, each excerpt
# size-capped; a typed note reports how many more remain pending (they surface on
# the following turn — never dropped).
_MAX_NOTIFY_BLOCKS = 8
_NOTIFY_EXCERPT_MAX = 600


def _sanitize_excerpt(text: str) -> str:
    """Neutralize a child-authored excerpt before it is embedded in the fenced
    notification block (#948 S6 adversarial-review [5]).

    The excerpt is the child agent's OWN final message text — untrusted content
    that may reflect a poisoned document / web page / tool output. Embedded raw
    inside triple-backtick fences under the block's marker, a child could close the
    fence and forge extra ``### task …`` rows or the notification marker into the
    parent's turn input (a fake-notification / cross-agent injection surface). This
    replaces the two STRUCTURAL tokens the child must not control — any run of three
    or more backticks (the fence delimiter) and the block marker — so child text can
    never break out of the fence or forge the marker. Content is otherwise preserved
    verbatim; the length bound is applied by the caller."""

    text = re.sub(r"`{3,}", "``", text)
    return text.replace(PENDING_TASK_NOTIFICATION_MARKER, "[marker removed]")


def _notify_block(task: Any) -> str:
    """Compose ONE agent-task notification block — a uniform structured field
    template (#948 S6 model-decides lock).

    Renders the SAME fields for every terminal task, success or failure alike:
    task id, child expert, status, typed error_reason, a size-bounded result
    excerpt, and the child session id. There is deliberately NO branch on the
    result's CONTENT (only the fixed excerpt length bound + structural-token
    sanitization) — a failed child's result is presented identically to a completed
    one, and the MODEL decides."""

    result = task.result or {}
    excerpt = _sanitize_excerpt(str(result.get("answer_excerpt", ""))[:_NOTIFY_EXCERPT_MAX])
    return (
        f"### task {task.task_id} — {task.agent_ref.get('expert_id', '')} [{task.status}]\n"
        f"- child_session_id: {task.child_session_id}\n"
        f"- error_reason: {task.error_reason}\n"
        f"- result_excerpt:\n```\n{excerpt}\n```"
    )


def inject_pending_agent_task_notifications(
    app: "FastAPI", sid: str, enriched_text: str
) -> tuple[str, list[str]]:
    """Prepend a bounded block of completed-but-unconsumed background task results
    to this turn's enriched input and STAGE (do not consume) the selected task ids
    (#948 S6 observe-later; adversarial-review [1]/[4]).

    An async child spawned in a PRIOR turn that was never collected (via
    wait/check) in that turn sets ``notify_pending`` at completion. Here — during
    the parent's next turn's ENRICHMENT — those results are composed into a
    server-grounding block (marked with :data:`PENDING_TASK_NOTIFICATION_MARKER`) so
    the model SEES them and decides what to do; clio never auto-acts on the content.

    Consumption is DEFERRED to the commit-to-run seam
    (:func:`consume_pending_agent_task_notifications`): this function only composes
    the block and RETURNS ``(text, selected_task_ids)`` so the caller stages the ids
    on the turn state WITHOUT consuming. If the turn then aborts after enrichment (a
    pre_message hook veto, a cancellation before forward), the tasks stay
    ``notify_pending`` and the NEXT turn injects them again — the observe-later
    guarantee is never at-most-once-dropped. Bounded to :data:`_MAX_NOTIFY_BLOCKS`;
    a typed note reports any remaining (they surface next turn — never dropped)."""

    from clio_agent.gact.agent_tasks import pending_notifications  # noqa: PLC0415

    pending = pending_notifications(app, sid)
    if not pending:
        return enriched_text, []
    selected = pending[:_MAX_NOTIFY_BLOCKS]
    blocks = [_notify_block(task) for task in selected]
    remaining = len(pending) - len(selected)
    truncation = (
        f"\n\n_({remaining} more finished task(s) pending — they will surface next turn.)_"
        if remaining > 0
        else ""
    )
    text = (
        PENDING_TASK_NOTIFICATION_MARKER
        + "\n\n"
        + "\n\n".join(blocks)
        + truncation
        + "\n\n---\n\n"
        + enriched_text
    )
    return text, [task.task_id for task in selected]


def consume_pending_agent_task_notifications(app: "FastAPI", sid: str, task_ids: list[str]) -> None:
    """Consume the observe-later notifications staged by
    :func:`inject_pending_agent_task_notifications` AND emit each one's delegation
    terminal — at the COMMIT-TO-RUN seam, never at compose time (#948 S6
    adversarial-review [1]/[4]).

    Called once the turn is committed to forward with the enriched input (after the
    last abort/veto seam), so a turn vetoed/aborted after enrichment leaves the
    tasks ``notify_pending`` for the next turn. For each staged task:

    * consume it exactly once (durable ``consumed_at`` + ``agent.task.consumed``),
      atomically via the ``notify_pending`` once-guard; and
    * emit its delegation TERMINAL — the SAME choreography ``wait_agent_tasks`` /
      ``check_agent_tasks`` emit (``blueprint.delegation.completed|failed`` + the
      return ``expert_handoff`` Part + ``blueprint.delegation.parent_resumed``),
      keyed to the parent expert that requested the child.

    The terminal emission goes through the SHARED ``delegation_reported`` once-gate
    (``_emit_delegation_terminal``), so if a later ``wait_agent_tasks`` also reaches
    this task it does not double-emit — and vice versa (exactly-once on the wire in
    either order). Without this, an async child collected only via observe-later
    left a ``blueprint.delegation.started`` with no terminal on the wire (a dangling
    delegation that renders the child perpetually in-progress). The return Part is
    appended to THIS (parent-session) turn's live transcript — the natural home,
    since the parent-session transcript is where the delegation renders."""

    if not task_ids:
        return
    from clio_agent.gact.agent_tasks import consume_notification  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        _runtime_active_agent_blueprint_id,
    )
    from clio_agent.gact.agents.spawn_runtime import _emit_delegation_terminal  # noqa: PLC0415
    from clio_agent.gact.types import AgentDef  # noqa: PLC0415

    reg = app.state.agent_task_registry
    blueprint_id = _runtime_active_agent_blueprint_id(app, sid) or ""
    for task_id in task_ids:
        task = reg.get(task_id)
        if task is None:
            continue
        # Consume (atomic once-guard); a concurrent wait may already have consumed
        # it, in which case this no-ops. The terminal emission below is separately
        # once-gated, so we ALWAYS attempt it (exactly-once regardless of order).
        claimed = consume_notification(app, task_id)
        if claimed is not None:
            from clio_agent.gact.background_exit import (  # noqa: PLC0415
                emit_background_exit_part,
            )

            emit_background_exit_part(app, sid, claimed)
        parent_id = task.agent_ref.get("requesting_expert_id", "") or "main"
        parent_def = AgentDef(
            id=parent_id,
            title=parent_id,
            metadata={"agent_blueprint_id": blueprint_id},
        )
        _emit_delegation_terminal(app, sid, parent_def, claimed or task)


def _context_file_turn_provenance(app: "FastAPI", sid: str, *, status: str) -> dict[str, Any]:
    """Return non-secret provenance for context files attached to this turn."""

    rows = list((app.state.context_files.get(sid, {}) or {}).values())
    files: list[dict[str, Any]] = []
    for row in rows:
        path = str(row.get("path") or "")
        if not path:
            continue
        mode = str(row.get("mode") or "read")
        file_row: dict[str, Any] = {
            "path": path,
            "mode": mode,
            "status": status,
            "inline_policy": "metadata_only" if mode == "edit" else "inline_or_inspect",
        }
        for key in ("source", "workspace_id", "display_path", "resolved_path", "added_at"):
            value = row.get(key)
            if value:
                file_row[key] = value
        if row.get("size") is not None:
            file_row["size"] = row.get("size")
        files.append(file_row)
    return {
        "status": status,
        "count": len(files),
        "max_inline_bytes": _CTX_MAX_BYTES,
        "files": files,
    }
