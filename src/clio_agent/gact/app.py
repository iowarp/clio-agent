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
import contextvars

# Diagnostic: SIGUSR1 dumps all thread tracebacks to stderr (for wedge debugging).
import faulthandler as _faulthandler  # noqa: E402
import fnmatch
import importlib.util
import inspect
import json
import logging
import os
import re
import shutil
import signal as _signal  # noqa: E402
import subprocess
import sys
import threading
import time
import uuid

_MEMPROF_STATE: dict[str, Any] = {"prev": None, "n": 0}


def _memprof_dump(signum: Any, frame: Any) -> None:
    """SIGUSR1 handler (when ``debug.memprof`` is on): dump a tracemalloc
    snapshot of the top allocations + a gc type histogram, for heap attribution.

    Writes to ``CLIO_DEBUG_MEMPROF_OUT.<n>.txt`` if set (numbered so successive
    SIGUSR1s can be diffed), else to stderr. Best-effort; never raises.
    """
    try:
        import collections
        import gc
        import tracemalloc

        snap = tracemalloc.take_snapshot()
        cur, peak = tracemalloc.get_traced_memory()
        try:
            with open(f"/proc/{os.getpid()}/status") as _f:
                rss = next(
                    (int(line.split()[1]) / 1024 for line in _f if line.startswith("VmRSS")),
                    -1.0,
                )
        except OSError:
            rss = -1.0
        lines = [
            f"pid={os.getpid()} RSS={rss:.1f}MB "
            f"traced_current={cur / 1e6:.1f}MB traced_peak={peak / 1e6:.1f}MB",
            "=== top 30 allocations by line ===",
        ]
        for stat in snap.statistics("lineno")[:30]:
            fr = stat.traceback[0]
            lines.append(
                f"{stat.size / 1e6:8.2f}MB count={stat.count:<8} {fr.filename}:{fr.lineno}"
            )
        prev = _MEMPROF_STATE["prev"]
        if prev is not None:
            lines.append("=== top 25 GROWTH since previous snapshot ===")
            for diff in snap.compare_to(prev, "lineno")[:25]:
                fr = diff.traceback[0]
                lines.append(
                    f"{diff.size_diff / 1e6:+8.2f}MB (count {diff.count_diff:+d}) "
                    f"{fr.filename}:{fr.lineno}"
                )
        lines.append("=== gc object type histogram (top 25) ===")
        hist = collections.Counter(type(o).__name__ for o in gc.get_objects())
        lines.extend(f"{count:>9}  {name}" for name, count in hist.most_common(25))
        _MEMPROF_STATE["prev"] = snap
        _MEMPROF_STATE["n"] += 1

        report = "\n".join(lines) + "\n"
        out = os.environ.get("CLIO_DEBUG_MEMPROF_OUT", "").strip()
        if out:
            with open(f"{out}.{_MEMPROF_STATE['n']}.txt", "w", encoding="utf-8") as fh:
                fh.write(report)
        else:
            sys.stderr.write("\n=== CLIO MEMPROF SNAPSHOT ===\n" + report)
            sys.stderr.flush()
    except Exception:  # noqa: BLE001 - diagnostics must never crash the server
        pass


def _install_sigusr1_diagnostic() -> None:
    """Install the SIGUSR1 diagnostic handler.

    Default: ``faulthandler`` thread-traceback dump (wedge debugging). When
    ``debug.memprof`` (env ``CLIO_DEBUG_MEMPROF``) is on, SIGUSR1 instead dumps a
    tracemalloc heap snapshot (heap attribution) — the in-core replacement for
    ad-hoc sitecustomize profiling, and it does not fight faulthandler.

    SIGUSR1 and ``faulthandler.register`` are POSIX-only — on Windows neither
    exists and merely referencing them raises ``AttributeError`` (not the
    ``ValueError``/``OSError`` guarded below), which would crash server import.
    This diagnostic is therefore a no-op on platforms without SIGUSR1.
    """
    if not hasattr(_signal, "SIGUSR1"):
        return
    memprof = False
    try:
        from clio_agent import conf

        memprof = conf.resolve(
            "debug.memprof", env="CLIO_DEBUG_MEMPROF", default=False, cast=conf.as_bool
        )
    except Exception:  # noqa: BLE001 - config must never block server import
        memprof = False
    if memprof:
        try:
            import tracemalloc

            frames = int(os.environ.get("CLIO_DEBUG_MEMPROF_FRAMES", "20") or "20")
            tracemalloc.start(frames)
        except Exception:  # noqa: BLE001
            pass
        try:
            _signal.signal(_signal.SIGUSR1, _memprof_dump)
        except (ValueError, OSError):
            pass
    else:
        try:
            _faulthandler.register(_signal.SIGUSR1, all_threads=True)
        except (ValueError, OSError):
            pass


_install_sigusr1_diagnostic()

from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Iterator, Literal, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from clio_agent import conf
from clio_agent.gact import context as _ctx
from clio_agent.gact.semantic_events import (
    DEFAULT_DETAIL_LEVEL,
    SemanticEventSink,
    build_trace_backend,
)
from clio_agent.gact.workspace_scope import (
    resolve_workspace_storage_root,
)
from clio_agent.prompts import PromptRegistry, PromptSource
from clio_agent.runtime import trace
from clio_agent.runtime.lm_activity import lm_call_in_flight as _lm_call_in_flight
from clio_agent.tools.file_policy import validate_write_path
from clio_agent.tools.fs_write import write_text_with_policy

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Runtime base re-export shim (#714 decomposition, step 1)                       #
#                                                                               #
# The shared runtime foundation (the ARC singleton + accessors, the semantic-   #
# event funnel, the internal exceptions, the id/timestamp + SSE helpers, and    #
# the ``_ctx`` boundary shims/caches) was carved out into                       #
# ``clio_agent.gact.runtime.globals`` -- the single source every other          #
# extracted module imports FROM (so nothing imports this 24k-line module; the   #
# graph stays acyclic). They are re-exported here so                            #
# ``from clio_agent.gact.app import <name>`` (and ``test_import_seams``) keep    #
# working unchanged. ``runtime.globals`` is the OWNER of ``_PROCESS_ARC`` -- it  #
# is re-exported as a name here, but all LIVE reads/writes happen inside         #
# ``runtime.globals`` (test patch/reset sites target it there).                 #
# --------------------------------------------------------------------------- #
from clio_agent.gact.runtime.globals import (  # noqa: E402, F401
    _ACTIVE_BLUEPRINT_TOOL_ROWS,
    _ACTIVE_GACT_APP,
    _ACTIVE_GACT_SESSION_ID,
    _ACTIVE_GACT_TRACE_ID,
    _ACTIVE_GACT_TURN_ID,
    _EXPERT_CHILDREN_CACHE,
    _ORCHESTRATOR_BRIEFING_CACHE,
    _PROCESS_ARC,
    ARC_OP_EVENT_TYPE,
    _active_lm_last_reasoning,
    _active_semantic_trace_id,
    _active_semantic_turn_id,
    _BlueprintTerminalWorkflowState,
    _build_semantic_event,
    _coerce_error_info,
    _CompatVar,
    _ContextFileAccessError,
    _emit_arc_op,
    _emit_expert_lifecycle_event,
    _emit_react_step_event,
    _emit_semantic_event,
    _format_sse,
    _gact_app_context,
    _iso_from_epoch,
    _jsonish,
    _llm_provider_payload,
    _new_attempt_id,
    _new_cancellation_attempt_id,
    _new_context_frame_id,
    _new_memory_event_id,
    _new_message_id,
    _new_part_id,
    _new_question_id,
    _not_implemented,
    _process_arc,
    _resolve_tool_session,
    _semantic_trace_id,
    _set_app_arc,
    _tool_session_context,
    _TurnCancelled,
    _TurnTimedOut,
    _UnsupportedSessionAgent,
    _wire_arc_op_logger,
    _with_ui_safe_semantic_fields,
)


def _prediction_summary(pred: Any) -> dict[str, Any]:
    summary = {
        "selected_expert": str(getattr(pred, "selected_expert", "") or ""),
        "route_source": str(getattr(pred, "route_source", "") or ""),
        "route_reason": str(
            getattr(pred, "route_reason", "") or getattr(pred, "routing_rationale", "") or ""
        ),
        "answer": str(getattr(pred, "answer", "") or ""),
        "expert_handoffs": _jsonish(getattr(pred, "expert_handoffs", None) or []),
        "tools_called": _jsonish(getattr(pred, "tools_called", None) or []),
        "file_diffs": _jsonish(getattr(pred, "file_diffs", None) or []),
        "error_info": _jsonish(getattr(pred, "error_info", None)),
    }
    # Full capture (durable trace): the dspy ReAct trajectory and the extract's
    # chain-of-thought reasoning. These are in SENSITIVE_KEYS, so the SSE
    # projection strips them while the canonical trace keeps them for debugging
    # and (later) re-extract repair. Only attach when present to keep the
    # routing/predict payloads lean.
    trajectory = getattr(pred, "trajectory", None)
    if trajectory:
        summary["trajectory"] = _jsonish(trajectory)
    reasoning = getattr(pred, "reasoning", None)
    if reasoning:
        summary["reasoning"] = str(reasoning)
    return summary


def _coerce_ask_user_action(pred: Any) -> dict[str, Any]:
    """Extract an ask-user planner action from a prediction-like object."""

    candidates = [
        getattr(pred, "ask_user", None),
        getattr(pred, "user_question", None),
        getattr(pred, "action", None),
    ]
    action_json = getattr(pred, "action_json", None)
    if isinstance(action_json, str) and action_json.strip():
        try:
            candidates.append(json.loads(action_json))
        except json.JSONDecodeError:
            pass
    for raw in candidates:
        if raw is None:
            continue
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                continue
        if not isinstance(raw, Mapping):
            continue
        action = str(raw.get("action") or raw.get("type") or "").strip().lower()
        if action and action not in {"ask_user", "question", "user_question"}:
            continue
        question = str(raw.get("question") or raw.get("prompt") or "").strip()
        if not question:
            continue
        choices_raw = raw.get("choices") or raw.get("options") or []
        choices = choices_raw if isinstance(choices_raw, list) else []
        return {
            "question": question,
            "choices": [c for c in choices if isinstance(c, Mapping)],
            "allow_freeform": bool(raw.get("allow_freeform", True)),
            "kind": str(raw.get("kind") or "").strip(),
            "reason": str(raw.get("reason") or raw.get("category") or "").strip(),
            "caller": raw.get("caller") if isinstance(raw.get("caller"), Mapping) else {},
            "metadata": raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {},
        }
    return {}


def _ask_user_options_from_action(action: Mapping[str, Any]) -> list["UserQuestionOption"]:
    options: list[UserQuestionOption] = []
    for idx, choice in enumerate(action.get("choices", []) or []):
        if not isinstance(choice, Mapping):
            continue
        label = str(choice.get("label") or choice.get("title") or choice.get("id") or "").strip()
        value = str(choice.get("value") or choice.get("id") or label).strip()
        description = str(choice.get("description") or "").strip()
        if not label:
            continue
        options.append(
            UserQuestionOption(
                label=label,
                value=value or f"choice_{idx + 1}",
                description=description,
            )
        )
    return options


def _ask_user_resume_text(question: "UserQuestion") -> str:
    selected = ", ".join(question.selected_options)
    answer = question.answer.strip()
    lines = [
        "[Answer to agent question]",
        f"Question: {question.prompt}",
    ]
    if selected:
        lines.append(f"Selected option(s): {selected}")
    if answer:
        lines.append(f"Answer: {answer}")
    return "\n".join(lines)


def _expert_handoff_summary(handoff: Mapping[str, Any]) -> str:
    """Return a compact user-facing summary for an expert handoff part."""

    agent = str(handoff.get("agent_id") or handoff.get("expert") or "expert")
    parent = str(handoff.get("parent_id") or handoff.get("parent") or "").strip()
    status = str(handoff.get("status") or "observed")
    stage = str(handoff.get("stage") or handoff.get("dispatch_target") or "").strip()
    output = str(handoff.get("output_summary") or handoff.get("summary") or "").strip()
    route = f"{parent} -> {agent}" if parent else agent
    bits = [route, status]
    if stage:
        bits.append(stage)
    if output:
        bits.append(output)
    return " | ".join(bits)


def _format_subagent_input(spawn_input: Any) -> str:
    """Format a materialized nanoagent input without a raw Python-dict look."""

    if isinstance(spawn_input, str):
        return spawn_input
    try:
        return "Subagent input:\n" + json.dumps(spawn_input, indent=2, sort_keys=True)
    except (TypeError, ValueError):
        return f"Subagent input:\n{spawn_input}"


def _compact_exact_evidence_index(transcript: str) -> str:
    """Build a deterministic evidence index to append to LM compact summaries."""
    paths: list[str] = []
    identifiers: list[str] = []
    caveats: list[str] = []

    def add_unique(target: list[str], value: str, *, limit: int) -> None:
        cleaned = " ".join(value.strip("`'\" \t\r\n,.;:()[]{}").split())
        cleaned = cleaned.rstrip("/")
        if not cleaned or cleaned in target:
            return
        if len(cleaned) > 180:
            cleaned = cleaned[:177] + "..."
        if len(target) < limit:
            target.append(cleaned)

    quoted = re.findall(r"`([^`]+)`", transcript)
    for item in quoted:
        if re.search(r"\.(?:h5|hdf5|parquet|csv|bp5|bp4|bp|sac|png|json|tar)\b", item, re.I):
            add_unique(paths, item, limit=40)
        elif re.search(r"[/_]", item) or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{2,}", item):
            add_unique(identifiers, item, limit=80)

    path_pattern = re.compile(
        r"(?:[A-Za-z]:\\[^\r\n`\"<>|]*?\.(?:h5|hdf5|parquet|csv|bp5|bp4|bp|sac|png|json|tar))"
        r"|(?:/[^\s`\"<>|]*?\.(?:h5|hdf5|parquet|csv|bp5|bp4|bp|sac|png|json|tar))",
        re.I,
    )
    for match in path_pattern.finditer(transcript):
        add_unique(paths, match.group(0), limit=40)

    identifier_pattern = re.compile(
        r"(?<![A-Za-z0-9])/?[A-Za-z][A-Za-z0-9]*(?:[_/.-][A-Za-z0-9]+)+\b",
    )
    for match in identifier_pattern.finditer(transcript):
        value = match.group(0)
        if len(value) < 4:
            continue
        if value.lower().startswith(("http", "https")):
            continue
        add_unique(identifiers, value, limit=80)

    caveat_terms = (
        "error",
        "failed",
        "missing",
        "unavailable",
        "not installed",
        "caveat",
        "unresolved",
        "follow-up",
        "follow up",
        "needs checking",
        "action needed",
    )
    for raw_line in transcript.splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        lowered = line.lower()
        if any(term in lowered for term in caveat_terms):
            add_unique(caveats, line, limit=16)

    sections: list[str] = []
    if paths:
        sections.append("Paths:\n" + "\n".join(f"- {path}" for path in paths))
    if identifiers:
        sections.append(
            "Identifiers:\n" + "\n".join(f"- {identifier}" for identifier in identifiers)
        )
    if caveats:
        sections.append("Caveats/errors:\n" + "\n".join(f"- {caveat}" for caveat in caveats))
    if not sections:
        return ""
    return "[exact retained evidence index]\n" + "\n\n".join(sections)


_EXECUTABLE_SESSION_AGENT_IDS = {
    "",
    "main",
    "default",
}


def _gact_turn_timeout_s(app: Optional["FastAPI"] = None) -> float:
    """Return the per-turn no-progress timeout in seconds; <=0 disables it.

    Precedence: a RUNTIME value set via ``PUT /v1/providers/lm`` (``turn_timeout_s``,
    stored on ``app.state.lm_config``) wins, so a client configures this on the
    SAME channel it configures the LM — no disconnected server-launch env. When
    unset (0/absent), fall back to the conf pathway (file → ``CLIO_GACT_TURN_TIMEOUT_S``
    → 900s default).
    """
    if app is not None:
        cfg = getattr(getattr(app, "state", None), "lm_config", None)
        if isinstance(cfg, Mapping):
            try:
                runtime = conf.as_float(cfg.get("turn_timeout_s") or 0)
            except (ValueError, TypeError):
                runtime = 0.0
            if runtime > 0:
                return runtime
    try:
        return conf.resolve(
            "limits.turn_timeout_s",
            env="CLIO_GACT_TURN_TIMEOUT_S",
            default=900.0,
            cast=conf.as_float,
        )
    except (ValueError, TypeError):
        return 900.0


def _keyword_user_agent_routing_enabled() -> bool:
    """Return whether legacy keyword routing into user agents is enabled."""

    raw = os.environ.get("CLIO_ENABLE_KEYWORD_USER_AGENT_ROUTING", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _agent_not_available_error(app: "FastAPI", sid: str) -> "ErrorEnvelope":
    """Return a typed error when no executable CLIO agent is ready for a turn."""

    task = getattr(app.state, "agent_construction_task", None)
    task_done = bool(getattr(task, "done", lambda: True)())
    init_error = str(getattr(app.state, "agent_init_error", "") or "")
    want_agent = bool(getattr(app.state, "want_agent", False))

    if want_agent and not task_done:
        status = "starting"
        message = "CLIO is still starting its agent; no agent is ready to accept messages yet."
        recoverable = True
        recovery_actions = ["wait_for_agent_startup", "retry", "check_health"]
    elif init_error:
        status = "failed"
        message = "CLIO agent startup failed; no agent is available to accept messages."
        recoverable = True
        recovery_actions = ["check_server_logs", "fix_lm_configuration", "restart_agent"]
    else:
        status = "not_configured"
        message = (
            "No executable CLIO agent is configured for this backend. Launch `clio-agent-gact` "
            "with an LM provider configured before sending messages."
        )
        recoverable = False
        recovery_actions = ["configure_lm_provider", "restart_agent"]

    details: dict[str, Any] = {
        "session_id": sid,
        "agent_status": status,
        "want_agent": want_agent,
        "recovery_actions": recovery_actions,
    }
    if init_error:
        details["agent_init_error"] = init_error

    return ErrorEnvelope(
        error=ErrorInfo(
            error="agent_not_available",
            message=message,
            details=details,
            recoverable=recoverable,
        )
    )


def _append_session_message(app: "FastAPI", session_id: str, message: "Message") -> None:
    """Append one chronological message to memory and disk."""

    app.state.messages.setdefault(session_id, []).append(message)
    store = getattr(app.state, "message_store", None)
    if store is not None:
        store.append(session_id, message)
    _mirror_workspace_messages(app, session_id)


def _extend_session_messages(
    app: "FastAPI",
    session_id: str,
    messages: list["Message"],
) -> None:
    """Append several chronological messages to memory and disk."""

    if not messages:
        return
    app.state.messages.setdefault(session_id, []).extend(messages)
    store = getattr(app.state, "message_store", None)
    if store is not None:
        store.extend(session_id, messages)
    _mirror_workspace_messages(app, session_id)


def _replace_session_messages(
    app: "FastAPI",
    session_id: str,
    messages: list["Message"],
) -> None:
    """Replace one session's message ledger in memory and disk."""

    app.state.messages[session_id] = list(messages)
    store = getattr(app.state, "message_store", None)
    if store is not None:
        store.replace_session(session_id, list(messages))
    _mirror_workspace_messages(app, session_id)


def _delete_session_messages(app: "FastAPI", session_id: str) -> None:
    """Remove one session's message ledger from memory and disk."""

    app.state.messages.pop(session_id, None)
    store = getattr(app.state, "message_store", None)
    if store is not None:
        store.delete_session(session_id)
    _mirror_workspace_messages(app, session_id)


def _release_session_arc(app: "FastAPI", session_id: str) -> None:
    """Release a closed session's hot footprint from ARC (best-effort).

    Persistence is write-through, so this only drops the in-memory cache/index
    copies; it never deletes durable records. Keeps an idle server from pinning
    every closed session's objects in the never-evicted hot path.
    """

    arc = getattr(app.state, "arc", None)
    if arc is None:
        return
    release = getattr(arc, "release_session", None)
    if release is None:
        return
    try:
        release(session_id)
    except Exception:  # noqa: BLE001 - lifecycle cleanup must never fail a request
        pass


def _workspace_for_session(app: "FastAPI", session_id: str) -> Any | None:
    sess = app.state.sessions.get(session_id)
    if sess is None:
        return None
    return app.state.workspaces.get(getattr(sess, "workspace_id", ""))


def _workspace_storage_root_for_session(app: "FastAPI", session_id: str) -> Path | None:
    ws = _workspace_for_session(app, session_id)
    if ws is None:
        return None
    return resolve_workspace_storage_root(ws)


def _mirror_workspace_session(app: "FastAPI", session_id: str) -> None:
    """Persist one session row into the owning workspace storage root."""

    sess = app.state.sessions.get(session_id)
    root = _workspace_storage_root_for_session(app, session_id)
    if sess is None or root is None:
        return
    path = root / "sessions.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:
                data = {}
        data[session_id] = asdict(sess)
        data[session_id].setdefault("metadata", {})
        data[session_id]["metadata"]["workspace_storage_root"] = str(root)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        return


def _mirror_workspace_messages(app: "FastAPI", session_id: str) -> None:
    """Persist one message ledger into the owning workspace storage root."""

    root = _workspace_storage_root_for_session(app, session_id)
    if root is None:
        return
    try:
        store = MessageStore(root / "messages")
        messages = list(app.state.messages.get(session_id, []))
        if messages:
            store.replace_session(session_id, messages)
        else:
            store.delete_session(session_id)
    except Exception:
        return


def _remove_workspace_session_mirror(app: "FastAPI", session_id: str) -> None:
    """Remove one mirrored session row from its workspace-local store."""

    root = _workspace_storage_root_for_session(app, session_id)
    if root is None:
        return
    try:
        sessions_path = root / "sessions.json"
        if sessions_path.exists():
            data = json.loads(sessions_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.pop(session_id, None)
                tmp = sessions_path.with_suffix(sessions_path.suffix + ".tmp")
                tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
                os.replace(tmp, sessions_path)
        MessageStore(root / "messages").delete_session(session_id)
    except Exception:
        return


# Multi-turn continuity: how many prior messages to carry and how much of each.
_HISTORY_MAX_MESSAGES = 6
_HISTORY_MAX_CHARS_PER_MESSAGE = 1200


def _compile_session_conversation_history(
    app: "FastAPI", session_id: str, current_prompt: str
) -> str:
    """Prepend a compact transcript of THIS session's prior turns to the turn's
    prompt so a multi-turn orchestrator can reuse what earlier turns already
    established (the resolved region, ranked stations, staged file paths) instead of
    restarting blind on a follow-up like "now plot it". General to any blueprint and
    a NO-OP on the first turn (no prior messages), so single-turn behaviour is
    unchanged. The orchestrator otherwise receives only the latest user message."""
    messages = list(app.state.messages.get(session_id, []))
    prior = [m for m in messages if getattr(m, "role", "") in {"user", "assistant"}]
    # The current user message is already appended before the turn runs — drop the
    # trailing user message(s) so only PRIOR turns are carried.
    while prior and prior[-1].role == "user":
        prior.pop()
    if not prior:
        return current_prompt
    lines: list[str] = []
    for message in prior[-_HISTORY_MAX_MESSAGES:]:
        text = _message_text_excerpt(message, max_chars=_HISTORY_MAX_CHARS_PER_MESSAGE)
        if not text:
            continue
        speaker = "User" if message.role == "user" else "Assistant"
        lines.append(f"{speaker}: {text}")
    if not lines:
        return current_prompt
    transcript = "\n".join(lines)
    return (
        "Earlier turns in THIS conversation — reuse what was already resolved "
        "(region/coordinates, ranked stations, staged file paths) rather than "
        "starting over; only the request after the marker is new:\n"
        f"{transcript}\n\n=== Current request ===\n{current_prompt}"
    )


def _load_context_files(path: Path | None) -> dict[str, dict[str, dict[str, Any]]]:
    """Load persisted context-file attachments keyed by session id."""

    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    sessions = data.get("sessions", {}) if isinstance(data, Mapping) else {}
    if not isinstance(sessions, Mapping):
        return {}
    loaded: dict[str, dict[str, dict[str, Any]]] = {}
    for sid, rows in sessions.items():
        if not isinstance(rows, Mapping):
            continue
        bucket: dict[str, dict[str, Any]] = {}
        for path_key, row in rows.items():
            if not isinstance(row, Mapping):
                continue
            path_value = str(row.get("path") or path_key or "").strip()
            if not path_value:
                continue
            bucket[path_value] = dict(row) | {"path": path_value}
        if bucket:
            loaded[str(sid)] = bucket
    return loaded


def _flush_context_files(app: "FastAPI") -> None:
    """Persist the current context-file ledger, if persistence is configured."""

    path = getattr(app.state, "context_files_path", None)
    if path is None:
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps({"sessions": app.state.context_files}, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)


def _delete_session_context_files(app: "FastAPI", session_id: str) -> None:
    """Remove one session's context-file ledger from memory and disk."""

    if session_id in app.state.context_files:
        app.state.context_files.pop(session_id, None)
        _flush_context_files(app)


def _session_not_found(sid: str) -> HTTPException:
    return HTTPException(
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


def _cancelled_error_info(
    sid: str,
    *,
    execution_cancellation: str,
    executor_work_may_continue: bool,
) -> "ErrorInfo":
    return ErrorInfo(
        error="cancelled",
        message="turn cancelled by client",
        details={
            "session_id": sid,
            "execution_cancellation": execution_cancellation,
            "executor_work_may_continue": executor_work_may_continue,
        },
        recoverable=True,
    )


def _cancellation_attempt_summary(attempt: Mapping[str, Any] | None) -> dict[str, Any]:
    if not attempt:
        return {}
    return {
        key: attempt[key]
        for key in (
            "id",
            "session_id",
            "requested_at",
            "in_flight",
            "cooperative_signal_sent",
            "asyncio_task_cancel_scheduled",
            "asyncio_task_cancel_sent",
            "hard_abort_supported",
            "upstream_abort",
            "executor_work_may_continue",
        )
        if key in attempt
    }


def _enrich_cancellation_error_info(
    app: "FastAPI",
    sid: str,
    error_info: "ErrorInfo | None",
) -> "ErrorInfo | None":
    """Attach durable cancellation-attempt evidence to cancelled turns."""

    if error_info is None or error_info.error != "cancelled":
        return error_info
    attempts = getattr(app.state, "cancel_attempts", None)
    attempt = attempts.get(sid) if isinstance(attempts, Mapping) else None
    if not attempt:
        return error_info
    details = error_info.details
    details.setdefault("cancellation_attempt_id", attempt.get("id", ""))
    details.setdefault("cancellation_attempt", _cancellation_attempt_summary(attempt))
    details.setdefault("hard_abort_supported", attempt.get("hard_abort_supported", False))
    details.setdefault("upstream_abort", attempt.get("upstream_abort", "not_supported"))
    return error_info


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


def _session_agent_id(sess: Any) -> str:
    """Return the active session agent id from dict or object refs."""

    agent = getattr(sess, "agent", None)
    if isinstance(agent, Mapping):
        return str(agent.get("id") or "").strip()
    return str(getattr(agent, "id", "") or "").strip()


# --------------------------------------------------------------------------- #
# Agent resolution + prompt composition re-export shims (#714 step 5/A).         #
#                                                                               #
# The stateless agent/blueprint/expert-pack RESOLUTION queries and the prompt   #
# COMPOSITION / dynamic-context renderers were carved out into                   #
# ``clio_agent.gact.agents.resolution`` and ``clio_agent.gact.agents.composition``#
# (each takes ``app`` explicitly; both import only the shared runtime base +     #
# gact leaves, never this module). They are re-exported here so                  #
# ``from clio_agent.gact.app import <name>`` keeps working unchanged. The owner  #
# modules are the single source of truth; tests that patch these must target the #
# owner (``...agents.resolution`` / ``...agents.composition``), not this shim.   #
# --------------------------------------------------------------------------- #
from clio_agent.gact.agents.composition import (  # noqa: E402, F401
    _agent_prompt_request,
    _agent_rows_prompt_render_context,
    _apply_prompt_registry_to_agent,
    _prompt_render_context,
    _prompt_resolution_metadata,
    _runtime_active_workspace_context,
    _runtime_dynamic_agent_children_context,
)
from clio_agent.gact.agents.resolution import (  # noqa: E402, F401
    _agent_definition_is_agent_blueprint,
    _agent_definition_uses_blueprint_runtime,
    _agent_overlay_patchable_fields,
    _legacy_native_expert_runtime_enabled,
    _merge_agent_def_rows,
    _resolve_dynamic_agent,
    _resolve_runtime_dynamic_agent,
    _runtime_active_agent_blueprint_agent_ids,
    _runtime_active_agent_blueprint_id,
    _runtime_active_agent_blueprint_path,
    _runtime_active_agent_blueprint_root_id,
    _runtime_active_agent_blueprint_rows,
    _runtime_active_session_expert_pack_id,
    _runtime_active_session_expert_pack_path,
    _runtime_apply_session_agent_overlay,
    _runtime_child_agent_rows,
    _runtime_declared_child_ids,
    _runtime_session_agent_overlay,
    _runtime_workspace_catalog_cwd,
)


def _keyword_routed_user_agent(app: "FastAPI", text: str) -> "AgentDef | None":
    """Return the best registered user agent whose keyword matches text.

    This intentionally ignores auto-discovered skills for now. Skills can be
    numerous and global, so implicit routing only uses agents the user
    registered directly in this CLIO backend.
    """

    normalized = f" {re.sub(r'[^a-z0-9_+-]+', ' ', text.lower())} "
    matches: list[tuple[int, str, AgentDef]] = []
    for row in app.state.user_agents.list():
        agent = AgentDef(**row.to_wire())
        for raw_keyword in agent.keywords:
            keyword = str(raw_keyword or "").strip().lower()
            if not keyword:
                continue
            needle = f" {re.sub(r'[^a-z0-9_+-]+', ' ', keyword)} "
            if needle.strip() and needle in normalized:
                matches.append((len(keyword), agent.id, agent))
                break
    if not matches:
        return None
    matches.sort(key=lambda item: (-item[0], item[1]))
    return matches[0][2]


def _coerce_expert_handoff_rows(value: Any) -> list[dict[str, Any]]:
    """Normalize model-returned expert handoff data into dict rows."""

    if value is None:
        return []
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    if isinstance(value, tuple):
        return [dict(row) for row in value if isinstance(row, Mapping)]
    if isinstance(value, str):
        text = value.strip()
        if not text or text in {"[]", "null", "None"}:
            return []
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
            text = re.sub(r"\s*```$", "", text).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"(\[[\s\S]*\])", text)
            if match is None:
                return []
            try:
                parsed = json.loads(match.group(1))
            except json.JSONDecodeError:
                return []
        return _coerce_expert_handoff_rows(parsed)
    return []


def _dynamic_parent_resume_prompt(
    original_request: str,
    parent_agent: "AgentDef",
    executed_handoffs: list[dict[str, Any]],
    declared_child_ids: set[str] | None = None,
) -> str:
    """Build the compact continuation prompt given back to a dynamic parent."""

    rows: list[str] = []
    merged_state: dict[str, Any] = {}
    completed_ids: list[str] = []
    for row in executed_handoffs:
        if str(row.get("stage") or "") != "delegate.completed":
            continue
        agent_id = str(row.get("agent_id") or row.get("delegate_to") or "")
        if agent_id and agent_id not in completed_ids:
            completed_ids.append(agent_id)
        status = str(row.get("status") or "")
        summary = str(
            row.get("return_output_summary")
            or row.get("output_summary")
            or row.get("summary")
            or ""
        ).strip()
        children = row.get("children")
        child_note = ""
        if isinstance(children, list) and children:
            child_note = f"; nested_child_events={len(children)}"
        rows.append(f"- {agent_id}: status={status}{child_note}; result={summary}")
        child_state = row.get("workflow_state")
        if isinstance(child_state, Mapping):
            _merge_workflow_state_mapping(merged_state, child_state)
    result_block = "\n".join(rows) or "- No completed child delegation results were returned."
    # Surface the MERGED typed workflow_state from the completed children, not just the
    # prose summaries. A child may put its key result ONLY in the typed field (e.g.
    # qwopus writes acquisition.metadata_path / station_catalog.station_ids into
    # workflow_state but not into its prose answer); without this the parent cannot see
    # the child already delivered, and re-delegates to it in a loop.
    state_block = ""
    if merged_state:
        state_block = (
            "\n\nAuthoritative typed workflow_state accumulated from the completed "
            "children — read these typed fields (e.g. acquisition.metadata_path, "
            "station_catalog.station_ids, acquisition.status, profile.status) to decide "
            "the next step. A child whose result already appears here is DONE; do NOT "
            "re-delegate to it:\n" + _workflow_state_payload(merged_state)
        )
    # Show the orchestrator its own progress as a visible to-do list, so it does not
    # have to track "which of my children have run" mentally across re-invocations
    # (small models lose that thread and finish early). This is reactive grounding
    # (showing state), not forced routing — the agent still decides the next hop, and
    # a child being "not yet run" is informational, not an order to run it.
    progress_block = ""
    if declared_child_ids:
        remaining = [c for c in sorted(declared_child_ids) if c not in completed_ids]
        progress_block = (
            "\n\nYour delegation progress this turn — "
            f"your child experts: {sorted(declared_child_ids)}; "
            f"already run: {completed_ids or '[]'}; "
            f"not yet run: {remaining or '[]'}. "
            "You are the orchestrator: keep delegating to the children this task still "
            "needs, and finish only when the work is genuinely complete. Not every child "
            "is needed for every request — use judgment: skip the ones the evidence makes "
            "unnecessary (e.g. analysis/visualization when there is no data staged), but "
            "do not finish prematurely while a needed step has not run."
        )
    return (
        f"Original user request:\n{original_request}\n\n"
        f"Returned child expert results for parent expert {parent_agent.id!r}:\n"
        f"{result_block}{state_block}{progress_block}\n\n"
        "Continue from these results. Decide the next step via your next_expert / "
        "next_task output: route to the next child the task still needs, or set "
        "next_expert='finish' and write the final answer when the work is genuinely "
        "complete. You MAY go back and re-invoke a child you already ran when you need "
        "MORE or DIFFERENT results from it (e.g. more candidates, a wider search, the "
        "next-ranked item, a retry with corrected arguments) — give it a NEW, specific "
        "sub-task that says what additional result you need. Only restriction: do NOT "
        "re-delegate to repeat work that is ALREADY captured in the typed workflow_state "
        "above (same task, same result already present) — that is a loop, not progress."
    )


def _iter_delegation_return_rows(rows: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Yield completed delegation rows, including nested child return rows."""

    for row in rows:
        if row.get("stage") == "delegate.completed":
            yield row
        children = row.get("children")
        if isinstance(children, list):
            child_rows = [child for child in children if isinstance(child, dict)]
            yield from _iter_delegation_return_rows(child_rows)


def _compact_dynamic_delegation_output(output: str, *, limit: int = 2200) -> str:
    """Compact child output while retaining exact evidence needed by parents."""

    raw_text = output.strip()
    state_blocks = _compact_workflow_state_blocks(raw_text)
    text = raw_text
    display_text = _strip_embedded_workflow_state_evidence(text)
    has_scan_limited_state = any(
        token in block
        for block in state_blocks
        for token in ('"scan_limited": true', '"profile_limited": true')
    )
    if has_scan_limited_state:
        return "Retained typed workflow state:\n" + "\n".join(state_blocks)
    if len(text) <= limit:
        if state_blocks and state_blocks[0] not in display_text:
            return f"{display_text.rstrip()}\n\nRetained typed workflow state:\n{state_blocks[0]}"
        return display_text
    evidence_index = _compact_exact_evidence_index(display_text)
    stat_lines: list[str] = []
    stat_terms = (
        "trace",
        "npts",
        "delta",
        "sampling",
        "min",
        "max",
        "mean",
        "std",
        "peak",
        "duration",
        "start time",
        "end time",
        "network",
        "station",
        "channel",
    )
    for raw_line in display_text.splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            continue
        lowered = line.lower()
        if any(term in lowered for term in stat_terms):
            if line not in stat_lines:
                stat_lines.append(line)
        if len(stat_lines) >= 24:
            break
    retained_blocks: list[str] = []
    if evidence_index:
        retained_blocks.append(evidence_index)
    if state_blocks:
        retained_blocks.append("Retained typed workflow state:\n" + "\n".join(state_blocks))
    if stat_lines:
        retained_blocks.append(
            "Retained numeric/trace evidence:\n" + "\n".join(f"- {line}" for line in stat_lines)
        )
    retained = "\n\n".join(retained_blocks)
    head_limit = max(800, limit // 2)
    tail_limit = max(500, limit - head_limit - len(retained) - 120)
    head = display_text[:head_limit].rstrip()
    tail = display_text[-tail_limit:].lstrip() if tail_limit > 0 else ""
    pieces = [head, "[...delegation output truncated; exact evidence retained below...]"]
    if retained:
        pieces.append(retained)
    if tail:
        pieces.append("[tail]\n" + tail)
    return "\n\n".join(pieces)


_ARTIFACT_PATH_TOKEN_RE = re.compile(r"[A-Za-z0-9_./~+-]+\.(?:csv|png)", re.IGNORECASE)
_ARTIFACT_PATH_MISSING_FRAMING_RE = re.compile(
    r"(not\s+(?:been\s+)?(?:staged|downloaded|available|present|found|created|generated|produced)|"
    r"no\s+(?:png|csv|plot|figure|file|artifact|local)\b|"
    r"does\s+not\s+exist|doesn'?t\s+exist|not\s+yet|is\s+blocked|blocked\s+because|"
    r"cannot\s+be|could\s+not\s+be|no\s+such\s+file|would\s+(?:need|be)|will\s+be|"
    r"written\s+to|saved\s+to|expected\s+(?:location|at)|placeholder|hypothetical|"
    r"once\s+(?:the|a)\b|to\s+be\s+(?:created|generated|written))",
    re.IGNORECASE,
)


def _is_remote_artifact_ref(value: str) -> bool:
    """Whether a path string is a remote/URL reference (never a local artifact)."""

    value = str(value or "")
    return value.startswith(("http://", "https://", "ftp://", "//")) or "://" in value


_VERIFIED_ARTIFACT_STATE_PATHS: tuple[tuple[str, ...], ...] = (
    # The analysis-ready staged station time-series CSV (never the metadata
    # catalog, which is recorded separately under acquisition.metadata_path).
    ("acquisition", "local_path"),
    # The rendered plot PNG.
    ("artifact", "path"),
    ("visualization", "path"),
    ("visualization", "plot_path"),
    ("visualization", "staged_plot_png"),
    # The profiled station CSV (same file as acquisition.local_path).
    ("profile", "path"),
)


def _verified_local_artifact_paths_by_ext(
    state: Mapping[str, Any],
) -> dict[str, list[str]]:
    """Collect the run's authoritative on-disk artifact paths from the specific
    typed workflow_state fields that name a produced deliverable (the staged
    station CSV and the rendered PNG), bucketed by lowercase extension.

    Only these declared fields are consulted — not an arbitrary walk — so that
    incidental on-disk files such as the staged metadata catalog
    (``acquisition.metadata_path``) never count as the deliverable artifact and
    never make the substitution ambiguous. These are the only artifact paths a
    final answer may legitimately cite; any other local csv/png path it presents
    as a produced artifact is a model confabulation."""

    found: dict[str, list[str]] = {"csv": [], "png": []}
    for section, key in _VERIFIED_ARTIFACT_STATE_PATHS:
        section_obj = state.get(section)
        if not isinstance(section_obj, Mapping):
            continue
        token = section_obj.get(key)
        if not isinstance(token, str):
            continue
        token = token.strip()
        if not token or _is_remote_artifact_ref(token):
            continue
        lowered = token.lower()
        for ext in ("csv", "png"):
            if lowered.endswith("." + ext):
                try:
                    on_disk = Path(token).is_file()
                except OSError:
                    on_disk = False
                if on_disk and token not in found[ext]:
                    found[ext].append(token)
    return found


def _ground_fabricated_local_artifact_paths(
    answer: str,
    state: Mapping[str, Any],
) -> str:
    """Replace fabricated local artifact (csv/png) path citations in a final
    answer with the run's verified on-disk artifact of the same type.

    The synthesis model sometimes derives a plausible-but-wrong local artifact
    filename (e.g. an invented ``.../plots/<station>_timeseries.png`` or a
    ``<csv>.png`` swap) instead of copying the exact tool-returned path, and on a
    data-blocked run it can cite a local csv/png that was never produced at all.
    Such a path does not exist on disk and misrepresents the deliverable. This
    generic pass — driven only by the typed workflow_state and the filesystem,
    with no station/region heuristics — corrects a non-existent local csv/png
    citation: it rewrites it to the single verified artifact of that type when
    exactly one exists, otherwise (nothing real to point at, e.g. a data-blocked
    run) it neutralizes the fabricated path with an explicit not-produced note.
    Remote source URLs and paths the answer honestly frames as
    missing/not-yet-created are left untouched."""

    if not answer:
        return answer
    verified = _verified_local_artifact_paths_by_ext(state)

    result = answer
    for match in list(_ARTIFACT_PATH_TOKEN_RE.finditer(answer)):
        token = match.group(0)
        if _is_remote_artifact_ref(token):
            continue
        try:
            if Path(token).is_file():
                continue
        except OSError:
            continue
        ext = token.rsplit(".", 1)[-1].lower()
        candidates = verified.get(ext) or []
        # Path-doubling / prefix-mangling: if the non-existent token EMBEDS exactly
        # one verified artifact path as a substring (e.g. the model emitted
        # ".../ndp-/home/.../ndp-staging/P473.csv" — a real path with a duplicated
        # prefix), collapse to that verified path. Generic; runs before the
        # ambiguity check so it still corrects when several artifacts exist.
        embedded = [c for c in candidates if c and c in token and c != token]
        if len(embedded) == 1:
            result = result.replace(token, embedded[0])
            continue
        if len(candidates) > 1:
            # Ambiguous which verified artifact was meant; leave text unchanged.
            continue
        # Respect honest "not produced / would be at <path>" framing.
        lo = max(0, match.start() - 160)
        hi = min(len(answer), match.end() + 160)
        if _ARTIFACT_PATH_MISSING_FRAMING_RE.search(answer[lo:hi]):
            continue
        if len(candidates) == 1:
            # Exactly one verified artifact of this type: correct the citation.
            result = result.replace(token, candidates[0])
        else:
            # No real local artifact of this type exists this run: drop the
            # fabricated path rather than present an unproduced file as real.
            result = result.replace(token, f"[no local {ext} artifact was produced this run]")
    return result


def _strip_embedded_workflow_state_evidence(text: str) -> str:
    """Remove raw machine-state blocks before building human evidence snippets."""

    if not text:
        return ""
    retained: list[str] = []
    skipping_state_list = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        lowered = line.casefold()
        if not line:
            skipping_state_list = False
            retained.append(raw_line)
            continue
        if "workflow_state" in lowered:
            skipping_state_list = True
            continue
        if (
            skipping_state_list
            and line.startswith(("-", "{"))
            and any(
                token in lowered
                for token in (
                    '"acquisition"',
                    '"resource_candidate"',
                    '"profile"',
                    '"artifact"',
                    '"visualization"',
                )
            )
        ):
            continue
        skipping_state_list = False
        retained.append(raw_line)
    return "\n".join(retained).strip()


def _user_facing_dynamic_evidence_summary(output: str) -> str:
    """Remove machine-retained evidence scaffolding from a user-facing fallback answer."""

    text = output.strip()
    if not text:
        return ""
    marker_positions = [
        index
        for marker in (
            "[...delegation output truncated; exact evidence retained below...]",
            "[exact retained evidence index]",
            "Retained typed workflow state:",
            "Retained delegation continuation contracts:",
            "Retained numeric/trace evidence:",
            "CLIO typed workflow state:",
            "CLIO merged nested typed workflow state:",
            "CLIO inferred typed tool state:",
            "CLIO inferred typed tool state from tool observations:",
        )
        if (index := text.find(marker)) >= 0
    ]
    if not marker_positions:
        return text
    visible = text[: min(marker_positions)].rstrip()
    lines = visible.splitlines()
    while lines and _looks_like_truncated_user_facing_tail(lines[-1]):
        lines.pop()
    return "\n".join(lines).rstrip()


def _looks_like_truncated_user_facing_tail(line: str) -> bool:
    """Return whether a line is likely an unfinished fragment before compact evidence."""

    stripped = line.strip()
    if not stripped:
        return True
    if stripped in {"**Ev", "Ev", "**Evidence", "Evidence"}:
        return True
    if stripped.startswith("**") and not stripped.endswith("**") and len(stripped) <= 40:
        return True
    if stripped.endswith(("**", "`")):
        return False
    return False


def _compact_workflow_state_blocks(text: str, *, limit: int = 8) -> list[str]:
    """Return reconciled typed state before output head/tail truncation."""

    state = _workflow_state_from_outputs([text])
    if not state:
        return []
    block = json.dumps({"workflow_state": state}, sort_keys=True, default=str)
    return [block][:limit]


def _latest_parent_resumed_output_summary(
    rows: list[dict[str, Any]],
    parent_id: str,
) -> str:
    """Return the latest compact output from a resumed delegated parent."""

    latest = ""
    stack = list(rows)
    while stack:
        row = stack.pop(0)
        if (
            str(row.get("agent_id") or "") == parent_id
            and str(row.get("stage") or "") == "parent.resumed"
        ):
            summary = str(row.get("output_summary") or row.get("summary") or "").strip()
            if summary:
                latest = summary
        children = row.get("children")
        if isinstance(children, list):
            stack.extend(child for child in children if isinstance(child, dict))
    return latest


def _latest_delegation_output_summary(rows: list[dict[str, Any]]) -> str:
    """Return the latest completed delegated child output from nested rows."""

    latest = ""
    for row in _iter_delegation_return_rows(rows):
        summary = str(row.get("output_summary") or row.get("summary") or "").strip()
        if summary:
            latest = summary
    return latest


def _append_nested_workflow_state(output: str, rows: list[dict[str, Any]]) -> str:
    """Append typed state found in nested completed child rows to a parent return."""

    outputs = [
        str(row.get("output_summary") or row.get("summary") or "").strip()
        for row in _iter_delegation_return_rows(rows)
        if str(row.get("output_summary") or row.get("summary") or "").strip()
    ]
    state = _workflow_state_from_outputs(outputs)
    _merge_workflow_state_mapping(state, _workflow_state_from_handoff_rows(rows))
    for tool_row in _tool_calls_from_handoff_rows(rows):
        row_state = tool_row.get("workflow_state")
        if isinstance(row_state, Mapping):
            _merge_workflow_state_mapping(state, row_state)
    if not state:
        return output
    block = _workflow_state_payload(state)
    if block in output:
        return output
    return f"{output.rstrip()}\n\nCLIO merged nested typed workflow state:\n{block}"


def _latest_completed_artifact_output_summary(rows: list[dict[str, Any]]) -> str:
    """Return the latest completed child output that contains final artifact evidence."""

    latest = ""
    stack = list(rows)
    while stack:
        row = stack.pop(0)
        if str(row.get("stage") or "") == "delegate.completed" and str(row.get("status") or "") in {
            "",
            "completed",
        }:
            summary = str(row.get("output_summary") or row.get("summary") or "").strip()
            if re.search(r"(?im)^\s*(?:FINAL_ARTIFACT|ARTIFACT)\s*:", summary):
                latest = summary
        children = row.get("children")
        if isinstance(children, list):
            stack.extend(child for child in children if isinstance(child, dict))
    return latest


def _latest_completed_child_output_summary(
    rows: list[dict[str, Any]],
    child_ids: Iterable[str],
) -> str:
    """Return the latest completed output from one of the named child experts."""

    target_ids = {str(child_id).strip() for child_id in child_ids if str(child_id).strip()}
    if not target_ids:
        return ""
    latest = ""
    for row in _iter_delegation_return_rows(rows):
        if (
            str(row.get("stage") or "") == "delegate.completed"
            and str(row.get("status") or "") in {"", "completed"}
            and str(row.get("agent_id") or row.get("delegate_to") or "").strip() in target_ids
        ):
            summary = str(row.get("output_summary") or row.get("summary") or "").strip()
            if summary:
                latest = summary
    return latest


def _latest_final_child_output_summary(rows: list[dict[str, Any]]) -> str:
    """Return completed synthesis/final-report output when a parent finalizes poorly."""

    return _latest_completed_child_output_summary(
        rows,
        ("synthesis", "final", "final_report", "report", "summary"),
    )


def _bubbled_child_evidence_output_summary(
    rows: list[dict[str, Any]],
    parent_id: str,
    declared_child_ids: Iterable[str],
) -> str:
    """Return the best child-subtree result for strict-depth parent completion."""

    return _latest_parent_resumed_output_summary(
        rows,
        parent_id,
    ) or _latest_completed_child_output_summary(rows, declared_child_ids)


def _is_empty_dynamic_agent_answer_error(exc: Exception) -> bool:
    """Return whether a dynamic expert failed only because it produced no answer."""

    return "returned an empty answer" in str(exc)


def _tool_result_preview(result: Any) -> str:
    if result is None:
        return "completed"
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001
        return str(result)


def _tool_result_is_error(result: Any) -> bool:
    if isinstance(result, Mapping):
        if result.get("error"):
            return True
        status = str(result.get("status") or "").strip().lower()
        if status in {"error", "failed", "failure"}:
            return True
        ok = result.get("ok")
        if ok is False:
            return True
    elif isinstance(result, str):
        normalized = result.strip().casefold()
        if normalized.startswith("{") and '"error"' in normalized:
            return True
        if any(
            token in normalized
            for token in (
                "file_not_found",
                "file does not exist",
                "tool_error",
                "status=error",
                "status=failed",
            )
        ):
            return True
    return False


_TOOL_TRAJECTORY_EVIDENCE_KEYS = (
    "observation",
    "observations",
    "result",
    "results",
    "output",
    "outputs",
    "response",
    "responses",
    "tool_result",
    "tool_results",
    "tool_output",
    "tool_outputs",
)


def _tool_agent_empty_answer_fallback(trajectory: Any, *, max_items: int = 6) -> str:
    """Return bounded tool evidence when a ReAct tool agent produced no answer."""

    if not trajectory:
        return ""

    evidence: list[tuple[str, Any]] = []

    def collect(label: str, value: Any) -> None:
        if len(evidence) >= max_items:
            return
        if _tool_result_is_error(value):
            return
        preview = _tool_result_preview(value).strip()
        normalized_preview = preview.rstrip(".").casefold()
        if not preview or normalized_preview == "completed":
            return
        evidence.append((label, value))

    def visit(label: str, value: Any) -> None:
        if len(evidence) >= max_items:
            return
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_label = f"{label}.{key}" if label else str(key)
                normalized_key = str(key).lower()
                if any(token in normalized_key for token in _TOOL_TRAJECTORY_EVIDENCE_KEYS):
                    collect(child_label, child)
                elif isinstance(child, Mapping | list | tuple):
                    visit(child_label, child)
            return
        if isinstance(value, list | tuple):
            for idx, item in enumerate(value):
                visit(f"{label}[{idx}]" if label else f"[{idx}]", item)

    visit("trajectory", trajectory)
    if not evidence:
        return ""

    lines = [
        "The tool-backed expert produced no final prose answer, but CLIO retained "
        "successful tool-grounded evidence from its ReAct trajectory.",
        "",
        "Retained tool observations:",
    ]
    for label, value in evidence:
        preview = _tool_result_preview(value).strip()
        if len(preview) > 1200:
            preview = f"{preview[:1200].rstrip()}..."
        lines.append(f"- {label}: {preview}")
    return "\n".join(lines)


# --- re-export shim (#714): pure workflow_state merge/normalize helpers ---
# Definitions live in clio_agent.gact.workflow_state.merge. Imported here so
# they remain resolvable as clio_agent.gact.app.<name> for the rest of this
# module and existing test seams. (behavior-preserving extraction)
from clio_agent.gact.workflow_state.merge import (  # noqa: E402,F401
    _TRAJECTORY_TOOL_ARGS_KEYS,
    _TRAJECTORY_TOOL_NAME_KEYS,
    _TRAJECTORY_TOOL_RESULT_KEYS,
    _UNICODE_PATH_HYPHENS,
    _merge_inferred_workflow_state,
    _merge_non_empty_mapping,
    _merge_workflow_state_mapping,
    _normalize_pathlike_text,
    _normalize_workflow_state_scalar,
    _normalize_workflow_state_section,
    _trajectory_key_index,
    _value_has_semantic_content,
    _workflow_status_rank,
)


def _extract_tools_called_from_trajectory(
    trajectory: Any,
    *,
    max_items: int = 32,
    max_result_chars: int = 12000,
) -> list[dict[str, Any]]:
    """Recover bounded tool-call evidence from DSPy ReAct trajectories.

    DSPy versions and adapters vary in trajectory shape. This intentionally
    accepts the common indexed mapping form (`tool_name_0`, `tool_args_0`,
    `observation_0`) and nested/list step forms while preserving enough result
    evidence for post-run scientific audit.
    """

    rows: list[dict[str, Any]] = []

    def bounded_result(value: Any) -> Any:
        if _is_bounded_tool_result(value):
            return value  # already bounded -> never re-wrap (idempotent)
        preview = _tool_result_preview(value)
        if len(preview) <= max_result_chars:
            return value
        return {
            "preview": preview[:max_result_chars].rstrip(),
            "truncated": True,
            "original_chars": len(preview),
        }

    def append_row(row: Mapping[str, Any]) -> None:
        if len(rows) >= max_items:
            return
        name = str(row.get("name") or row.get("tool") or "").strip()
        result = row.get("result")
        args = row.get("args")
        if not name and result is None:
            return
        out: dict[str, Any] = {}
        if name:
            out["name"] = name
        if args is not None:
            out["args"] = args
        if result is not None:
            out["result"] = bounded_result(result)
            out["ok"] = not _tool_result_is_error(result)
        out.setdefault("telemetry_source", "agent_trajectory")
        rows.append(out)

    def visit(value: Any) -> None:
        if len(rows) >= max_items:
            return
        if isinstance(value, Mapping):
            # Direct step row: {"tool_name": ..., "tool_args": ..., "observation": ...}
            direct: dict[str, Any] = {}
            for key in _TRAJECTORY_TOOL_NAME_KEYS:
                if key in value:
                    direct["name"] = value[key]
                    break
            for key in _TRAJECTORY_TOOL_ARGS_KEYS:
                if key in value:
                    direct["args"] = value[key]
                    break
            for key in _TRAJECTORY_TOOL_RESULT_KEYS:
                if key in value:
                    direct["result"] = value[key]
                    break
            if direct:
                append_row(direct)
                for raw_key, child in value.items():
                    normalized_key = str(raw_key).lower()
                    if (
                        _trajectory_key_index(normalized_key, _TRAJECTORY_TOOL_NAME_KEYS)
                        is not None
                        or _trajectory_key_index(normalized_key, _TRAJECTORY_TOOL_ARGS_KEYS)
                        is not None
                        or _trajectory_key_index(normalized_key, _TRAJECTORY_TOOL_RESULT_KEYS)
                        is not None
                    ):
                        continue
                    if isinstance(child, Mapping | list | tuple):
                        visit(child)
                return

            # Indexed flat row: {"step_0_tool_name": ..., "step_0_observation": ...}
            indexed: dict[str, dict[str, Any]] = {}
            for raw_key, child in value.items():
                key = str(raw_key)
                name_index = _trajectory_key_index(key, _TRAJECTORY_TOOL_NAME_KEYS)
                if name_index is not None:
                    indexed.setdefault(name_index, {})["name"] = child
                    continue
                args_index = _trajectory_key_index(key, _TRAJECTORY_TOOL_ARGS_KEYS)
                if args_index is not None:
                    indexed.setdefault(args_index, {})["args"] = child
                    continue
                result_index = _trajectory_key_index(key, _TRAJECTORY_TOOL_RESULT_KEYS)
                if result_index is not None:
                    indexed.setdefault(result_index, {})["result"] = child
                    continue
                if isinstance(child, Mapping | list | tuple):
                    visit(child)
            for index in sorted(indexed, key=lambda item: int(item) if item.isdigit() else -1):
                append_row(indexed[index])
            return
        if isinstance(value, list | tuple):
            for child in value:
                visit(child)

    visit(trajectory)
    return rows


def _propose_edit_diffs_from_pred(pred: Any) -> list[dict[str, Any]]:
    """Promote successful ``fs_propose_edit`` tool results into file-diff proposals.

    A dynamic tool agent calls ``fs_propose_edit`` as a TOOL; unlike the builtin
    edit experts it does not populate ``pred.file_diffs``, so the returned
    proposal (path + unified_diff + new_content) never became a ``file_diff``
    part or a pending ``/v1/sessions/{sid}/diffs`` row — the TUI could see the
    tool call but never the diff (iowarp/clio-agent#674). Recover the proposals
    from the turn's tool results so the standard materialization picks them up.

    Reads ``pred.tools_called`` (which already carries each call's structured
    result), falling back to parsing ``pred.trajectory``. Only successful
    (``ok``) calls whose result carries a ``path`` and a diff/new_content are
    promoted; duplicates by (path, diff-prefix) are collapsed.
    """

    rows: list[Any] = list(getattr(pred, "tools_called", None) or [])
    if not rows:
        rows = _extract_tools_called_from_trajectory(getattr(pred, "trajectory", None))
    diffs: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = str(row.get("name") or row.get("tool") or "")
        if "propose_edit" not in name:
            continue
        if row.get("ok") is False:
            continue
        result = row.get("result")
        if not isinstance(result, Mapping):
            continue
        path = str(result.get("path") or "").strip()
        unified_diff = result.get("unified_diff") or ""
        new_content = result.get("new_content")
        if not path or (not unified_diff and new_content is None):
            continue
        key = (path, str(unified_diff)[:64])
        if key in seen:
            continue
        seen.add(key)
        diff: dict[str, Any] = {"path": path, "unified_diff": unified_diff}
        if new_content is not None:
            diff["new_content"] = new_content
        for extra in ("edit_mode", "lines_added", "lines_removed"):
            if extra in result:
                diff[extra] = result[extra]
        diffs.append(diff)
    return diffs


def _dynamic_agent_runtime_provenance(
    app: "FastAPI",
    agent_def: "AgentDef",
    *,
    execution_mode: str,
) -> dict[str, Any]:
    """Return non-secret provenance for the dynamic agent used this turn."""

    active_model = _active_lm_model_ref(app)
    provider_id = agent_def.default_provider or active_model.get("provider_id", "")
    model_id = agent_def.default_model or active_model.get("model_id", "")
    payload: dict[str, Any] = {
        "kind": "dynamic_agent",
        "agent_id": agent_def.id,
        "source": agent_def.source,
        "title": agent_def.title,
        "execution_mode": execution_mode,
        "module": dict(agent_def.module),
        "tools": list(agent_def.tools),
        "structured_outputs": dict(agent_def.structured_outputs),
        "fanout": dict(agent_def.fanout),
        "prompt": {
            "source": "agent_definition",
            "has_system_prompt": bool(agent_def.system_prompt.strip()),
        },
        "model": {
            "provider_id": provider_id,
            "model_id": model_id,
            "provider_source": ("agent_default" if agent_def.default_provider else "global_active"),
            "model_source": "agent_default" if agent_def.default_model else "global_active",
            "fallback_to_global": not (agent_def.default_provider and agent_def.default_model),
        },
    }
    blueprint_id = str(agent_def.metadata.get("agent_blueprint_id") or "").strip()
    if blueprint_id:
        payload["agent_blueprint"] = {
            "id": blueprint_id,
            "version": str(agent_def.metadata.get("agent_blueprint_version") or ""),
            "scope": str(agent_def.metadata.get("agent_blueprint_scope") or ""),
            "definition_path": str(agent_def.metadata.get("agent_blueprint_definition_path") or ""),
        }
    overlay = agent_def.metadata.get("agent_blueprint_overlay")
    if isinstance(overlay, Mapping):
        payload["agent_overlay"] = dict(overlay)
        fields = (
            set(overlay.get("fields") or []) if isinstance(overlay.get("fields"), list) else set()
        )
        if "system_prompt" in fields:
            payload["prompt"]["source"] = "session_agent_overlay"
    if agent_def.source == "expert_pack":
        payload.update(
            {
                "parent_id": agent_def.parent_id,
                "skills": list(agent_def.skills),
                "commands": list(agent_def.commands),
                "pack": {
                    "id": str(agent_def.metadata.get("pack_id") or ""),
                    "version": str(agent_def.metadata.get("pack_version") or ""),
                    "scope": str(
                        agent_def.metadata.get("pack_scope")
                        or agent_def.metadata.get("expert_scope")
                        or ""
                    ),
                    "definition_path": str(
                        agent_def.metadata.get("definition_path")
                        or agent_def.metadata.get("pack_definition_path")
                        or agent_def.metadata.get("expert_path")
                        or ""
                    ),
                },
            }
        )
    return payload


def _delegated_expert_agent_id(row: Mapping[str, Any]) -> str:
    """Return the requested delegated expert id from a handoff row."""

    for key in ("delegate_to", "agent_id", "target_agent_id", "expert"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _delegated_expert_prompt(row: Mapping[str, Any], fallback: str) -> str:
    """Build the child prompt for a synchronous expert delegation."""

    fallback = fallback.strip()
    for key in ("question", "input", "prompt", "request"):
        value = str(row.get(key) or "").strip()
        if value:
            if not fallback or fallback in value:
                return value
            evidence = fallback
            if len(evidence) > 2500:
                evidence = f"{evidence[:2500].rstrip()}..."
            return "\n\n".join(
                (
                    value,
                    "Parent evidence available for this delegated task:",
                    evidence,
                )
            )
    return fallback


def _append_accumulated_workflow_state_context(prompt: str, state: Mapping[str, Any]) -> str:
    """Attach durable typed state to child prompts without relying on prose."""

    if not state:
        return prompt
    block = (
        "Accumulated typed workflow state from prior CLIO tool evidence "
        "(authoritative; use this before local prose summaries):\n"
        f"{json.dumps({'workflow_state': state}, sort_keys=True, default=str)}"
    )
    if block in prompt:
        return prompt
    return "\n\n".join(part for part in (prompt.strip(), block) if part)


def _append_session_workflow_state_context(
    app: Any,
    session_id: str,
    prompt: str,
) -> str:
    """Attach accumulated session tool state to a delegated expert prompt."""

    ledger = getattr(getattr(app, "state", None), "tool_call_ledger", None)
    if not isinstance(ledger, dict):
        return prompt
    rows = ledger.get(session_id)
    if not isinstance(rows, list):
        return prompt
    prior_rows = [row for row in rows if isinstance(row, Mapping)]
    state: dict[str, Any] = {}
    for row in prior_rows:
        row_state = row.get("workflow_state")
        if isinstance(row_state, Mapping):
            _merge_workflow_state_mapping(state, row_state)
    if not state:
        return prompt
    return _append_accumulated_workflow_state_context(prompt, state)


def _active_lm_supports_vision(app: "FastAPI") -> bool:
    """Return whether the active provider transport can carry image parts."""

    cfg = _effective_lm_config(app)
    if "supports_vision" in cfg:
        return bool(cfg.get("supports_vision"))
    return str(cfg.get("provider") or "") in {"openai", "anthropic"}


def _agent_accepts_images(agent: Any) -> bool:
    """Return whether agent.forward can receive native image inputs."""

    forward = getattr(agent, "forward", None)
    if not callable(forward):
        return False
    try:
        params = inspect.signature(forward).parameters
    except (TypeError, ValueError):
        return False
    if "images" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _image_part_error(
    *,
    session_id: str,
    image_count: int,
    provider: Mapping[str, Any],
) -> ErrorEnvelope:
    provider_id = str(provider.get("provider") or provider.get("provider_id") or "")
    model_id = str(provider.get("model") or provider.get("model_id") or "")
    return ErrorEnvelope(
        error=ErrorInfo(
            error="unsupported_multimodal_image",
            message=(
                "The active LM provider cannot receive image message parts. "
                "Switch to a vision-capable direct provider or remove the image."
            ),
            details={
                "session_id": session_id,
                "image_part_count": image_count,
                "provider": provider_id,
                "model": model_id,
                "supports_vision": False,
                "recovery_actions": [
                    "switch_to_openai_or_anthropic",
                    "remove_image_part",
                    "attach_image_as_context_file_for_tool_inspection",
                ],
            },
            recoverable=True,
        )
    )


def _user_message_parts(
    *,
    request_parts: list["Part"],
    user_text: str,
) -> list["Part"]:
    """Return transcript parts for a user turn, preserving image parts."""

    if not request_parts:
        return [Part(id=_new_part_id(), type="text", text=user_text)]
    parts: list[Part] = []
    has_text = False
    for part in request_parts:
        if part.type not in {"text", "image"}:
            continue
        metadata = dict(part.metadata)
        if part.type == "image":
            metadata.setdefault("clio_multimodal", "preserved")
        copied = part.model_copy(
            update={
                "id": part.id or _new_part_id(),
                "metadata": metadata,
            }
        )
        if copied.type == "text" and copied.text:
            has_text = True
        parts.append(copied)
    if not has_text and user_text:
        parts.insert(0, Part(id=_new_part_id(), type="text", text=user_text))
    return parts or [Part(id=_new_part_id(), type="text", text=user_text)]


def _image_part_summaries(parts: list["Part"]) -> list[dict[str, Any]]:
    """Return bounded metadata for image parts without logging raw base64."""

    rows: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        if part.type != "image":
            continue
        rows.append(
            {
                "index": index,
                "id": part.id,
                "media_type": part.media_type or part.metadata.get("media_type", ""),
                "has_data": bool(part.data),
                "data_length": len(part.data or ""),
                "url": part.url,
                "metadata": {
                    key: value
                    for key, value in part.metadata.items()
                    if key not in {"data", "base64", "file"}
                },
            }
        )
    return rows


def _dspy_images_from_parts(parts: list["Part"]) -> list[Any]:
    """Convert GACT image parts to DSPy image inputs for native vision models."""

    images: list[Any] = []
    for part in parts:
        if part.type != "image":
            continue
        try:
            import dspy  # noqa: PLC0415

            if part.url:
                images.append(dspy.Image(part.url))
                continue
            if part.data:
                data = part.data
                if data.startswith("data:"):
                    images.append(dspy.Image(data))
                    continue
                media_type = part.media_type or part.metadata.get("media_type") or "image/png"
                images.append(dspy.Image(f"data:{media_type};base64,{data}"))
        except Exception:
            continue
    return images


def _should_execute_delegated_handoff(row: Mapping[str, Any]) -> bool:
    status = str(row.get("status") or "").strip().lower()
    if status in {"skipped", "failed", "cancelled", "completed"}:
        return False
    if row.get("execute") is False:
        return False
    if row.get("execute") is True or row.get("delegate_to") or row.get("target_agent_id"):
        return True
    return status in {"requested", "pending", "delegate", "delegated"}


def _json_objects_from_text(text: str) -> list[Any]:
    """Extract JSON objects embedded in model/tool evidence without trusting prose."""

    stripped = text.strip()
    objects: list[Any] = []
    decoder = json.JSONDecoder()
    if stripped.startswith(("{", "[")):
        try:
            objects.append(json.loads(stripped))
            return objects
        except json.JSONDecodeError:
            pass
    index = 0
    while index < len(text):
        if text[index] not in "{[":
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            index += 1
            continue
        objects.append(value)
        index += max(end, 1)
    return objects


def _merge_workflow_state_from_value(value: Any, state: dict[str, Any]) -> None:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("{", "[")):
            for nested in _json_objects_from_text(text):
                _merge_workflow_state_from_value(nested, state)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _merge_workflow_state_from_value(item, state)
        return
    if not isinstance(value, Mapping):
        # A typed workflow_state field may arrive as a Pydantic model when a pack
        # declares it as a nested object signature field. Convert it to a plain
        # mapping so its sections merge. Generic across all packs.
        if callable(getattr(value, "model_dump", None)):
            normalized = _jsonish(value)
            if isinstance(normalized, Mapping):
                _merge_workflow_state_from_value(normalized, state)
        return
    for key in ("workflow_state", "semantic_state", "state"):
        nested = value.get(key)
        if isinstance(nested, Mapping):
            _merge_workflow_state_mapping(state, nested)
    structured = value.get("structured")
    if isinstance(structured, Mapping):
        for nested in structured.values():
            _merge_workflow_state_from_value(nested, state)
    for key, nested in value.items():
        if key in {"workflow_state", "semantic_state", "state", "structured"}:
            continue
        if isinstance(nested, Mapping):
            if str(key) == "provenance":
                _merge_workflow_state_mapping(state, nested)
            _merge_workflow_state_mapping(state, {str(key): nested})


def _workflow_state_from_outputs(completed_outputs: list[Any]) -> dict[str, Any]:
    state: dict[str, Any] = {}
    for output in completed_outputs:
        if isinstance(output, str):
            for obj in _json_objects_from_text(output):
                _merge_workflow_state_from_value(obj, state)
        elif output is not None:
            _merge_workflow_state_from_value(output, state)
    return state


def _workflow_state_payload(state: Mapping[str, Any]) -> str:
    """Return a parseable workflow-state payload for prompts and compact rows."""

    return json.dumps({"workflow_state": state}, sort_keys=True, default=str)


def _workflow_state_from_handoff_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Return durable typed state stored on handoff rows and nested tool rows."""

    state: dict[str, Any] = {}

    def visit(row: Any) -> None:
        if not isinstance(row, Mapping):
            return
        raw_state = row.get("workflow_state")
        if isinstance(raw_state, Mapping):
            _merge_workflow_state_mapping(state, raw_state)
        for output_key in ("output_summary", "summary"):
            output = str(row.get(output_key) or "").strip()
            if output:
                _merge_workflow_state_mapping(state, _workflow_state_from_outputs([output]))
        for call in row.get("tools_called") or []:
            if isinstance(call, Mapping):
                call_state = call.get("workflow_state")
                if isinstance(call_state, Mapping):
                    _merge_workflow_state_mapping(state, call_state)
        for child in row.get("children") or []:
            visit(child)

    for row in rows:
        visit(row)
    return state


def _failed_child_delegation_workflow_state(
    *,
    prompt: str,
    child_agent_id: str,
    parent_agent_id: str,
    error: str,
    message: str,
    tools_called: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build typed state for a child failure without discarding prior evidence."""

    state = _workflow_state_from_outputs([prompt])
    for tool_row in tools_called:
        row_state = tool_row.get("workflow_state")
        if isinstance(row_state, Mapping):
            _merge_workflow_state_mapping(state, row_state)
    state["delegation"] = {
        "status": "failed",
        "failed_child": child_agent_id,
        "parent": parent_agent_id,
        "error": error,
        "message": message,
    }
    acquisition = state.get("acquisition")
    if isinstance(acquisition, dict) and acquisition.get("analysis_ready") is not True:
        acquisition["status"] = "blocked"
        acquisition["analysis_ready"] = False
        acquisition["blocker"] = (
            f"child expert {child_agent_id!r} failed before completing acquisition: {error}"
        )
    resource_discovery = state.get("resource_discovery")
    if isinstance(resource_discovery, dict):
        resource_discovery["status"] = "child_failed"
        resource_discovery["blocker"] = (
            f"child expert {child_agent_id!r} failed before completing resource discovery"
        )
        resource_discovery["next_action"] = (
            "retry the child expert after provider availability is restored"
        )
    return state


def _failed_child_delegation_output_summary(
    *,
    child_agent_id: str,
    parent_agent_id: str,
    error: str,
    message: str,
    workflow_state: Mapping[str, Any],
) -> str:
    """Return compact parent-consumable text for a failed child expert."""

    return (
        f"Child expert {child_agent_id!r} failed while delegated from "
        f"{parent_agent_id!r}: {error}. {message}\n\n"
        f"CLIO durable typed workflow state:\n{_workflow_state_payload(workflow_state)}"
    )


def _state_path_value(state: Mapping[str, Any], path: str) -> Any:
    current: Any = state
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return None
    return current


def _workflow_state_has_existing_staged_path(state: Mapping[str, Any]) -> bool:
    acquisition = state.get("acquisition")
    if not isinstance(acquisition, Mapping):
        return True
    status = str(acquisition.get("status") or "").strip().lower()
    if status != "staged" or acquisition.get("analysis_ready") is not True:
        return True
    local_path = str(acquisition.get("local_path") or acquisition.get("path") or "").strip()
    if not local_path.startswith(("/", "~")):
        return True
    return Path(local_path).expanduser().is_file()


def _state_predicate_hit(actual: Any, expected: Any) -> bool:
    if isinstance(expected, Mapping):
        if "exists" in expected:
            return (actual is not None) is bool(expected.get("exists"))
        if "equals" in expected:
            return _state_predicate_hit(actual, expected.get("equals"))
        if "in" in expected and isinstance(expected.get("in"), list | tuple | set):
            return any(_state_predicate_hit(actual, item) for item in expected["in"])
        if "not" in expected:
            return not _state_predicate_hit(actual, expected.get("not"))
    if isinstance(expected, list | tuple | set):
        return any(_state_predicate_hit(actual, item) for item in expected)
    if isinstance(actual, bool):
        if isinstance(expected, str):
            return actual is (expected.strip().lower() in {"1", "true", "yes", "on"})
        return actual is bool(expected)
    return str(actual).strip().lower() == str(expected).strip().lower()


def _user_agent_param(agent_def: "AgentDef", name: str) -> Any:
    """Return one user-agent generation parameter, if present."""
    params = agent_def.parameters if isinstance(agent_def.parameters, Mapping) else {}
    return params.get(name)


def _user_agent_int_param(agent_def: "AgentDef", name: str, default: int) -> int:
    """Parse an integer user-agent parameter with an explicit error."""
    value = _user_agent_param(agent_def, name)
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"user agent parameter {name!r} must be an integer") from exc


def _user_agent_bool_param(agent_def: "AgentDef", name: str, default: bool = False) -> bool:
    """Parse a boolean user-agent parameter."""

    value = _user_agent_param(agent_def, name)
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "allow", "allowed"}:
        return True
    if normalized in {"0", "false", "no", "off", "deny", "denied"}:
        return False
    return default


def _user_agent_float_param(agent_def: "AgentDef", name: str, default: float) -> float:
    """Parse a float user-agent parameter with an explicit error."""
    value = _user_agent_param(agent_def, name)
    if value in (None, ""):
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"user agent parameter {name!r} must be a number") from exc


# Expert runtime engine extracted to gact/agents/runtime.py (#714 decomposition
# step 4). Re-exported here so existing ``from clio_agent.gact.app import <name>``
# callers + the import-seam guardrail stay green; the kept turn-handler dispatch
# wrappers below (``_blueprint_runner_for_agent`` / ``_run_*``) reach the builders
# through these shims.
from clio_agent.gact.agents.builders import (  # noqa: E402,F401
    _active_base_agent_tool_executor,
    _adapter_tool_intent_from_exception,
    _blueprint_fanout_config,
    _blueprint_runtime_signature,
    _build_blueprint_dspy_module,
    _build_child_expert_tool,
    _build_fanout_tool,
    _build_prompt_user_agent_module,
    _build_tool_user_agent_module,
    _call_enabled_external_mcp_tool,
    _call_recovered_dspy_tool,
    _coerce_fanout_child_ids,
    _dynamic_agent_lm_config,
    _dynamic_agent_tools,
    _dynamic_child_expert_tools,
    _emit_blueprint_llm_failure,
    _emit_invalid_tool_selection_event,
    _enabled_external_mcp_dspy_tools,
    _extract_repair_attempts,
    _invalid_tool_selection_from_exception,
    _is_repairable_typed_output_error,
    _prompt_user_agent_signature,
    _recording_blueprint_tool,
    _recover_blueprint_react_tool_intent,
    _reextract_over_retained_trajectory,
    _repair_temperature,
    _run_external_mcp_tool_sync,
    _tool_names,
    _tool_user_agent_max_iters,
    _tool_user_agent_signature,
    _typed_output_repair_hint,
)
from clio_agent.gact.agents.runtime import (  # noqa: E402,F401
    _prediction_structured_metadata,
    _retaining_react_cls,
    _summarize_segments_llm,
)


def _append_prediction_workflow_state(output: str, result: Any) -> str:
    """Append a blueprint prediction's first-class workflow_state output."""

    raw_state = getattr(result, "workflow_state", None)
    if raw_state in (None, ""):
        return output
    if isinstance(raw_state, str):
        text = raw_state.strip()
        if not text:
            return output
        block = text
    else:
        # A typed workflow_state output field may arrive as a Pydantic model
        # (when a pack declares it as a nested object signature field) or as a
        # plain dict. Normalize any model to JSON-able structures so the nested
        # object survives serialization and downstream parsing instead of being
        # stringified into an unparseable repr. This is generic for all packs.
        normalized_state = _jsonish(raw_state)
        payload = (
            normalized_state
            if isinstance(normalized_state, Mapping) and "workflow_state" in normalized_state
            else {"workflow_state": normalized_state}
        )
        block = json.dumps(payload, sort_keys=True, default=str)
    if block in output:
        return output
    return f"{output.rstrip()}\n\nCLIO typed workflow state:\n{block}".strip()


def _fallback_answer_from_delegation(handoffs: list[dict[str, Any]]) -> str:
    """Return the latest compact parent-resume output as answer fallback."""

    for row in reversed(handoffs):
        if str(row.get("stage") or "") != "parent.resumed":
            continue
        if str(row.get("status") or "") not in {"", "completed"}:
            continue
        text = str(row.get("output_summary") or "").strip()
        if text:
            return text
    return ""


# Provider / LM-bind helpers moved to gact/providers/ (#714 decomposition step 6).
# Re-exported here so existing ``from clio_agent.gact.app import <name>`` callers +
# the import-seam guardrail (``_refresh_argonne_lm_token`` pinned) stay green; the
# write-side ``PUT /v1/providers/lm`` bind closures still live in the provider route
# handler below and move with the route extraction (step 7).
from clio_agent.gact.providers.auth import (  # noqa: E402,F401
    _is_placeholder_api_key,
    _refresh_argonne_lm_token,
    _resolve_argonne_runtime_api_key,
)
from clio_agent.gact.providers.config import (  # noqa: E402,F401
    _effective_lm_config,
    _provider_runtime_kind,
)
from clio_agent.gact.providers.lmstudio import (  # noqa: E402,F401
    _lm_studio_api_root,
    _lm_studio_headers,
    _release_owned_lm_studio_instance,
)
from clio_agent.gact.routes.agents import (  # noqa: E402
    register_agents_routes,
)
from clio_agent.gact.routes.blueprints import (  # noqa: E402
    register_blueprints_routes,
)
from clio_agent.gact.routes.catalog import (  # noqa: E402
    register_catalog_routes,
)
from clio_agent.gact.routes.context import (  # noqa: E402
    register_context_routes,
)
from clio_agent.gact.routes.deps import GactDeps  # noqa: E402
from clio_agent.gact.routes.diffs import (  # noqa: E402
    register_diffs_routes,
)
from clio_agent.gact.routes.expert_packs import (  # noqa: E402
    register_expert_packs_routes,
)
from clio_agent.gact.routes.hooks import (  # noqa: E402
    register_hooks_routes,
)
from clio_agent.gact.routes.mcp import (  # noqa: E402
    register_mcp_routes,
)
from clio_agent.gact.routes.memory import (  # noqa: E402
    register_memory_routes,
)
from clio_agent.gact.routes.misc import (  # noqa: E402
    register_misc_routes,
)
from clio_agent.gact.routes.permissions import (  # noqa: E402
    register_permissions_routes,
)
from clio_agent.gact.routes.prompts import (  # noqa: E402
    register_prompts_routes,
)
from clio_agent.gact.routes.schedules import (  # noqa: E402
    register_schedules_routes,
)
from clio_agent.gact.routes.system import (  # noqa: E402
    register_system_routes,
)
from clio_agent.gact.routes.workspaces import (  # noqa: E402
    register_workspaces_routes,
)

# Capability + metrics catalogs (the stream-fallback reason catalog, the
# capability-gap rows, and the latency-stat percentile helper) moved to
# gact/runtime/capabilities.py (#714 decomposition) so the read-only system
# routes (routes/system.py) and the message-turn streaming path here share one
# source. The turn path reads ``_STREAM_FALLBACK_REASON_DEFINITIONS`` via
# ``_stream_fallback_payload`` below; re-exported so existing
# ``from clio_agent.gact.app import <name>`` callers stay green.
from clio_agent.gact.runtime.capabilities import (  # noqa: E402,F401
    _CAPABILITY_GAP_DEFINITIONS,
    _STREAM_FALLBACK_REASON_DEFINITIONS,
    _capability_gap_metadata,
    _latency_stat,
    _stream_fallback_reason_capabilities,
)

# Slash-command table assembly moved to gact/runtime/commands.py (#714
# decomposition) so routes/catalog.py and the prompt-render-context closure here
# share one source. Imported under the legacy underscore names the render-context
# closure already used.
from clio_agent.gact.runtime.commands import (  # noqa: E402
    BACKEND_COMMANDS as _BACKEND_COMMANDS,
)
from clio_agent.gact.runtime.commands import (  # noqa: E402
    command_cwd_for_request as _command_cwd_for_request,
)
from clio_agent.gact.runtime.commands import (  # noqa: E402
    planner_command_rows as _planner_command_rows,
)

# Server-wide wire + limit constants (contract/backend version, inline-context
# byte cap) moved to gact/runtime/constants.py (#714 decomposition) so the route
# modules read them without importing back into app.py. Re-exported so existing
# ``from clio_agent.gact.app import <name>`` callers stay green.
from clio_agent.gact.runtime.constants import (  # noqa: E402,F401
    _CTX_MAX_BYTES,
    CONTRACT_VERSION,
    GACT_BACKEND_VERSION,
)

# Token / context-window leaf machinery moved to gact/runtime/context_tokens.py
# (#714 decomposition step 2). Re-exported here so existing
# ``from clio_agent.gact.app import <name>`` callers + the import-seam guardrail
# stay green; the expert forward (step 4) imports these from the new module.
from clio_agent.gact.runtime.context_tokens import (  # noqa: E402,F401
    _CONTEXT_CATEGORY,
    _arc_obs_value,
    _autocompact_threshold,
    _bucket_context_categories,
    _estimate_text_tokens,
    _last_prompt_tokens,
    _resolve_expert_context_window,
)

# Transcript-memory search primitives (query normalization, excerpting, the
# scope-controlled ranked search) + the shared message-excerpt projection moved
# to gact/runtime/memory_search.py (#714 decomposition) so the agent-run path
# (_enrich_with_requested_memory_search / _compile_session_conversation_history)
# and the memory routes (routes/memory.py) share one implementation. Re-exported
# here so existing ``from clio_agent.gact.app import <name>`` callers stay green.
from clio_agent.gact.runtime.memory_search import (  # noqa: E402,F401
    _memory_search_excerpt,
    _memory_search_response,
    _memory_search_terms,
    _message_text_excerpt,
)

# Permission-policy data machinery (validation, load/flush, resolution-derived
# policy + the constants) moved to gact/runtime/permission_policies.py (#714
# decomposition step 7) so the permissions route module + this startup path share
# one implementation. Re-exported here so existing
# ``from clio_agent.gact.app import <name>`` callers stay green; the in-app gate
# enforcement (_policy_action_for_tool / _guard_direct_destructive_action) and
# build_app startup import these from the new module.
from clio_agent.gact.runtime.permission_policies import (  # noqa: E402,F401
    _PERMISSION_POLICY_ACTIONS,
    _PERMISSION_POLICY_SCOPES,
    _append_permission_policy_from_resolution,
    _flush_permission_policies,
    _load_permission_policies,
    _permission_path_from_args,
    _validate_permission_policies,
)
from clio_agent.gact.runtime.type_parsing import (  # noqa: E402,F401
    _SCALAR_FIELD_TYPES,
    _blueprint_module_kind,
    _is_optional_annotation,
    _parse_field_annotation,
    _sanitize_model_name,
)


def _run_blueprint_dspy_agent(
    base_agent: Any,
    agent_def: "AgentDef",
    question: str,
    session_id: str,
    cancel_requested: Any | None = None,
) -> Any:
    token = _ctx.set_session_id(session_id)
    try:
        module = _build_blueprint_dspy_module(base_agent, agent_def)
        return module(
            question=question,
            session_id=session_id,
            cancel_requested=cancel_requested,
        )
    finally:
        _ctx.reset(token)


def _blueprint_runner_for_agent(agent_def: "AgentDef") -> Any:
    if _agent_definition_uses_blueprint_runtime(agent_def):
        return _run_blueprint_dspy_agent
    return _run_tool_user_agent if agent_def.tools else _run_prompt_user_agent


def _run_prompt_user_agent(
    base_agent: Any,
    agent_def: "AgentDef",
    question: str,
    session_id: str,
    cancel_requested: Any | None = None,
) -> Any:
    """Execute a prompt-only user/skill agent through DSPy/LiteLLM."""
    token = _ctx.set_session_id(session_id)
    try:
        module = _build_prompt_user_agent_module(base_agent, agent_def)
        return module.forward(
            question=question,
            session_id=session_id,
            cancel_requested=cancel_requested,
        )
    finally:
        _ctx.reset(token)


def _run_tool_user_agent(
    base_agent: Any,
    agent_def: "AgentDef",
    question: str,
    session_id: str,
    cancel_requested: Any | None = None,
) -> Any:
    """Execute a tool-declaring user/skill agent through DSPy ReAct."""
    token = _ctx.set_session_id(session_id)
    try:
        module = _build_tool_user_agent_module(base_agent, agent_def)
        return module.forward(
            question=question,
            session_id=session_id,
            cancel_requested=cancel_requested,
        )
    finally:
        _ctx.reset(token)


def _tool_call_event_key(call: Mapping[str, Any]) -> tuple[str, str]:
    """Return a stable identity for de-duplicating tool telemetry events."""
    call_id = str(call.get("call_id") or "").strip()
    if call_id:
        return "__call_id__", call_id
    return _tool_call_name_args_key(call)


def _tool_call_name_args_key(call: Mapping[str, Any]) -> tuple[str, str]:
    """Return a tool-name/arguments identity for posthoc trajectory rows."""

    name = str(call.get("name") or call.get("tool") or "")
    args = call.get("args")
    if args is None:
        args = call.get("arguments")
    if args is None:
        args = call.get("params")
    try:
        encoded_args = json.dumps(args or {}, sort_keys=True, default=str)
    except TypeError:
        encoded_args = str(args or {})
    return name, encoded_args


def _tool_call_has_result_evidence(call: Mapping[str, Any]) -> bool:
    """Return whether a tool-call row carries auditable result evidence."""

    for key in ("result", "observation", "output", "response", "result_preview"):
        value = call.get(key)
        if value in (None, "", [], {}):
            continue
        return True
    return False


def _is_bounded_tool_result(value: Any) -> bool:
    """True if ``value`` is already a bounded-preview payload.

    Bounding must be IDEMPOTENT: a bounded result
    (``{"preview": ..., "truncated": True, "original_chars": N}``) flows across
    stages (tool -> catalog -> data -> main) and was being re-bounded at each hop,
    nesting preview-of-preview-of-preview (observed: a geo_filter result wrapped
    22x by turn.completed, burying the real data so the staged station could not be
    verified in-region). Detecting an already-bounded payload here stops the nesting.
    """
    return (
        isinstance(value, Mapping)
        and value.get("truncated") is True
        and "preview" in value
        and "original_chars" in value
    )


def _bounded_tool_call_result(value: Any, *, max_result_chars: int = 12000) -> Any:
    """Return a JSON-safe bounded result payload for assistant metadata."""

    if _is_bounded_tool_result(value):
        return value  # already bounded -> never re-wrap (idempotent)
    preview = _tool_result_preview(value)
    if len(preview) <= max_result_chars:
        return value
    return {
        "preview": preview[:max_result_chars].rstrip(),
        "truncated": True,
        "original_chars": len(preview),
    }


def _normalize_tool_call_row(call: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a tool-call row while preserving bounded result evidence."""

    row: dict[str, Any] = {}
    call_id = str(call.get("call_id") or "").strip()
    if call_id:
        row["call_id"] = call_id
    name = call.get("name") or call.get("tool")
    if name:
        row["name"] = str(name)
    args = call.get("args")
    if args is None:
        args = call.get("arguments")
    if args is None:
        args = call.get("params")
    if args is not None:
        row["args"] = args
    for key in ("ok", "duration_ms", "cached", "error", "telemetry_source"):
        if key in call:
            row[key] = call[key]
    for key in ("result", "observation", "output", "response", "result_preview"):
        if key not in call:
            continue
        value = call.get(key)
        if value in (None, "", [], {}):
            continue
        if key == "result":
            row["result"] = _bounded_tool_call_result(value)
        else:
            row[key] = _bounded_tool_call_result(value)
        break
    if row and "telemetry_source" not in row:
        row["telemetry_source"] = "posthoc_prediction"
    return row


def _merge_tool_call_rows(
    primary_rows: list[dict[str, Any]],
    supplemental_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge tool-call telemetry without dropping richer result evidence."""

    merged: list[dict[str, Any]] = [_normalize_tool_call_row(row) for row in primary_rows if row]
    by_key: dict[tuple[str, str], list[int]] = {}
    by_name_args: dict[tuple[str, str], list[int]] = {}
    for index, row in enumerate(merged):
        by_key.setdefault(_tool_call_event_key(row), []).append(index)
        by_name_args.setdefault(_tool_call_name_args_key(row), []).append(index)

    for raw_supplemental in supplemental_rows:
        supplemental = _normalize_tool_call_row(raw_supplemental)
        if not supplemental:
            continue
        key = _tool_call_event_key(supplemental)
        candidate_index: int | None = None
        supplemental_has_result = _tool_call_has_result_evidence(supplemental)
        supplemental_ok = supplemental.get("ok")
        candidate_indexes = list(by_key.get(key, []))
        if not candidate_indexes and (
            not supplemental.get("call_id") or not supplemental_has_result
        ):
            fallback_indexes = by_name_args.get(_tool_call_name_args_key(supplemental), [])
            if supplemental_has_result:
                fallback_indexes = [
                    index for index in fallback_indexes if merged[index].get("ok") is not False
                ]
            if len(fallback_indexes) == 1:
                candidate_indexes = fallback_indexes
        for index in candidate_indexes:
            existing = merged[index]
            existing_ok = existing.get("ok")
            if key[0] == "__call_id__":
                candidate_index = index
                break
            if supplemental_has_result and existing_ok is False and supplemental_ok is not False:
                continue
            if supplemental_has_result and not _tool_call_has_result_evidence(existing):
                candidate_index = index
                break
            if not supplemental_has_result:
                candidate_index = index
                break
        if candidate_index is None:
            by_key.setdefault(key, []).append(len(merged))
            by_name_args.setdefault(_tool_call_name_args_key(supplemental), []).append(len(merged))
            merged.append(supplemental)
            continue

        existing = merged[candidate_index]
        for field_name, value in supplemental.items():
            if field_name in {"result", "observation", "output", "response", "result_preview"}:
                if not _tool_call_has_result_evidence(existing):
                    existing[field_name] = value
                continue
            if value in (None, "", [], {}):
                continue
            if field_name not in existing or existing[field_name] in (None, "", [], {}):
                existing[field_name] = value
            elif field_name in {"duration_ms", "cached", "telemetry_source", "ok", "error"}:
                existing[field_name] = value
    return merged


def _tool_calls_from_handoff_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return nested child tool-call evidence from delegation rows."""

    tool_rows: list[dict[str, Any]] = []

    def visit(row: Any) -> None:
        if not isinstance(row, Mapping):
            return
        for call in row.get("tools_called") or []:
            if isinstance(call, Mapping):
                tool_rows.append(_normalize_tool_call_row(call))
        for child in row.get("children") or []:
            visit(child)

    for row in rows:
        visit(row)
    return tool_rows


def _model_ref_dict(value: Any) -> dict[str, str]:
    """Normalize a GACT ModelRef-like value to its wire keys."""

    if value is None:
        raw: Mapping[str, Any] = {}
    elif isinstance(value, Mapping):
        raw = value
    elif hasattr(value, "model_dump"):
        raw = value.model_dump(exclude_none=True)
    else:
        raw = {
            "provider_id": getattr(value, "provider_id", ""),
            "model_id": getattr(value, "model_id", ""),
            "variant": getattr(value, "variant", ""),
        }
    return {
        "provider_id": str(raw.get("provider_id") or raw.get("provider") or ""),
        "model_id": str(raw.get("model_id") or raw.get("model") or ""),
        "variant": str(raw.get("variant") or ""),
    }


def _model_ref_is_empty(value: Any) -> bool:
    """Return true when a model ref carries no selection."""

    ref = _model_ref_dict(value)
    return not any(ref.values())


def _active_lm_model_ref(app: "FastAPI") -> dict[str, str]:
    """Return the active global LM as a GACT ModelRef-shaped dict."""

    cfg = _effective_lm_config(app)
    provider = str(cfg.get("provider") or "")
    model = str(cfg.get("model") or "")
    return {"provider_id": provider, "model_id": model, "variant": ""}


def _model_ref_matches_active(value: Any, app: "FastAPI") -> bool:
    """Return true when a requested model ref exactly matches the active LM."""

    return _model_ref_dict(value) == _active_lm_model_ref(app)


def _clear_session_model_refs(app: "FastAPI") -> None:
    """Clear per-session model refs after a global LM provider swap.

    CLIO executes every turn through the active global LM. Existing
    sessions may still carry stale GACT ModelRefs from older TUI
    versions or emulator-compatible defaults; leaving those refs in
    place makes the next send fail with a per-session override error
    even though the user just changed the global provider correctly.
    """

    sessions = getattr(app.state, "sessions", None)
    if sessions is None:
        return
    for sess in sessions.list():
        if not _model_ref_is_empty(sess.model):
            sessions.update(sess.id, model={})


def _unsupported_model_ref_error(
    *,
    session_id: str,
    source: str,
    model_ref: Any,
    active_model: Mapping[str, str],
) -> ErrorEnvelope:
    """Build a structured error for currently unsupported model refs."""

    return ErrorEnvelope(
        error=ErrorInfo(
            error="not_implemented",
            message=(
                f"{source} model overrides are not implemented for a model "
                "that differs from the active global LM."
            ),
            details={
                "session_id": session_id,
                "source": source,
                "model": _model_ref_dict(model_ref),
                "active_model": dict(active_model),
                "recovery_actions": [
                    "put_global_lm_provider",
                    "clear_session_model",
                    "retry",
                    "exit",
                ],
            },
            recoverable=True,
        )
    )


async def _run_turn_in_background(
    app: "FastAPI",
    sid: str,
    user_text: str,
    user_msg: "Message",
    turn_agent_id: str = "",
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
    route_source = ""
    route_reason = ""
    auto_routed_agent: "AgentDef | None" = None
    agent_runtime: dict[str, Any] = {}
    dynamic_agent_used: "AgentDef | None" = None
    execution_path = ""
    tools_called: list[dict[str, Any]] = []
    expert_handoffs: list[dict[str, Any]] = []
    prompt_resolution: dict[str, Any] = {}
    proposed_diffs: list[Any] = []
    nanoagents: list[Any] = []
    thinking_text = ""
    retry_attempt_id = ""
    if isinstance(user_msg.metadata, dict):
        retry_attempt_id = str(user_msg.metadata.get("retry_attempt_id") or "")
    turn_id = user_msg.id
    trace_id = _semantic_trace_id(turn_id)
    # Bare sets, no reset: turn_id/trace_id must stay live for every later
    # copy_context() snapshot taken during this turn (mirrors the original
    # turn-scoped leak). app/session are established independently. (#714)
    _ctx.set_turn_id(turn_id)
    _ctx.set_trace_id(trace_id)
    native_images = _dspy_images_from_parts(user_msg.parts)
    turn_tokens: dict[str, int] = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write": 0,
    }
    turn_cost = 0.0

    def _update_retry_attempt(
        status: str,
        *,
        metadata_patch: Optional[dict[str, Any]] = None,
    ) -> None:
        if not retry_attempt_id:
            return
        attempt = app.state.turn_attempts.get(retry_attempt_id)
        if attempt is None:
            return
        metadata = dict(attempt.metadata)
        if metadata_patch:
            metadata.update(metadata_patch)
        updated = attempt.model_copy(
            update={
                "status": status,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "metadata": metadata,
            }
        )
        app.state.turn_attempts[retry_attempt_id] = updated
        app.state.bus.publish(
            Event(
                type=f"turn.retry_{status}",
                session_id=sid,
                payload=updated.model_dump(exclude_none=True),
            )
        )

    if retry_attempt_id:
        _update_retry_attempt(
            "running",
            metadata_patch={"executed_user_message_id": user_msg.id},
        )
    _emit_semantic_event(
        app,
        sid,
        "turn.started",
        turn_id=turn_id,
        trace_id=trace_id,
        status="running",
        summary="User turn accepted and CLIO runtime started.",
        actor={"role": "user"},
        subject={"message_id": user_msg.id},
        payload={"text": user_text, "retry_attempt_id": retry_attempt_id},
    )

    # iowarp/clio-agent#5: prepend any attached context files to the
    # user's text so the agent's forward() sees them as primed input.
    # Plain text concat — keeps the agent.py interface untouched and
    # works regardless of which expert handles the turn.
    context_file_error: ErrorInfo | None = None
    context_file_provenance = _context_file_turn_provenance(app, sid, status="prepared")
    memory_search_metadata: dict[str, Any] = {}
    try:
        enriched_text = _enrich_with_context_files(app, sid, user_text)
        enriched_text, memory_search_metadata = _enrich_with_requested_memory_search(
            app,
            sid,
            enriched_text,
            user_msg,
        )
        # Carry prior turns of this session so a follow-up ("now plot it") can reuse
        # the region/stations/paths already resolved. No-op on the first turn.
        enriched_text = _compile_session_conversation_history(app, sid, enriched_text)
    except _ContextFileAccessError as exc:
        enriched_text = user_text
        context_file_error = exc.error_info
        context_file_provenance = _context_file_turn_provenance(app, sid, status="error")
    context_frame = _record_context_frame(
        app,
        sid,
        sess,
        user_msg,
        user_text=user_text,
        enriched_text=enriched_text,
        context_error=context_file_error,
    )
    if memory_search_metadata:
        _emit_semantic_event(
            app,
            sid,
            "memory.search.completed",
            turn_id=turn_id,
            trace_id=trace_id,
            summary="Requested memory search was injected into turn context.",
            actor={"role": "runtime", "component": "memory"},
            subject={"message_id": user_msg.id},
            payload=memory_search_metadata,
        )
    # iowarp/clio-agent#20: pre_message hook can transform the
    # input or veto the turn. PermissionError → cancelled-style
    # error_info; the caller sees the hook's reason.
    if context_file_error is None:
        try:
            from clio_agent.runtime.hooks import fire as _fire_hook

            _emit_semantic_event(
                app,
                sid,
                "hook.invocation.started",
                turn_id=turn_id,
                trace_id=trace_id,
                status="running",
                summary="pre_message hook dispatch started.",
                actor={"hook": "pre_message"},
                subject={"message_id": user_msg.id},
                payload={"input": enriched_text},
            )
            hook_scope = {
                "session_id": sid,
                "workspace_id": getattr(sess, "workspace_id", ""),
                "blueprint_id": _runtime_active_agent_blueprint_id(app, sid),
            }
            _fire_hook("pre_message", sid, enriched_text, hook_scope=hook_scope)
            _emit_semantic_event(
                app,
                sid,
                "hook.invocation.completed",
                turn_id=turn_id,
                trace_id=trace_id,
                summary="pre_message hook dispatch completed.",
                actor={"hook": "pre_message"},
                subject={"message_id": user_msg.id},
                payload={},
            )
        except PermissionError as exc:
            _emit_semantic_event(
                app,
                sid,
                "hook.pre_message.blocked",
                turn_id=turn_id,
                trace_id=trace_id,
                status="blocked",
                summary="pre_message hook blocked the turn.",
                actor={"hook": "pre_message"},
                subject={"message_id": user_msg.id},
                payload={"error": str(exc)},
            )
            _emit_semantic_event(
                app,
                sid,
                "turn.failed",
                turn_id=turn_id,
                trace_id=trace_id,
                status="blocked",
                summary="CLIO turn was blocked by pre_message hook.",
                actor={"hook": "pre_message"},
                subject={"message_id": user_msg.id},
                payload={"error": str(exc)},
            )
            bus.publish(
                Event(
                    type="message.completed",
                    session_id=sid,
                    payload={
                        "turn_id": turn_id,
                        "message_id": user_msg.id,
                        "stop_reason": "blocked",
                        "error_info": {
                            "error": "permission_error",
                            "message": str(exc),
                            "recoverable": True,
                        },
                    },
                )
            )
            app.state.sessions.update(sid, status="error")
            _update_retry_attempt(
                "failed",
                metadata_patch={
                    "execution_error": "permission_error",
                    "executed_user_message_id": user_msg.id,
                },
            )
            bus.publish(
                Event(
                    type="session.status_changed",
                    session_id=sid,
                    payload={
                        "session_id": sid,
                        "status": "error",
                        "prev_status": "running",
                        "reason": "pre_message hook blocked turn",
                    },
                )
            )
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
            live_ids = getattr(app.state, "live_assistant_message_ids", {}) or {}
            streamed_assistant_msg_id = str(live_ids.get(sid) or "")
            created_live_msg = bool(streamed_assistant_msg_id)
            if not streamed_assistant_msg_id:
                streamed_assistant_msg_id = _new_message_id("asst")
                live_ids[sid] = streamed_assistant_msg_id
                app.state.live_assistant_message_ids = live_ids
            streamed_assistant_part_id = _new_part_id()
            if not created_live_msg:
                bus.publish(
                    Event(
                        type="message.created",
                        session_id=sid,
                        payload=Message(
                            id=streamed_assistant_msg_id,
                            turn_id=turn_id,
                            session_id=sid,
                            role="assistant",
                            created_at=_iso_from_epoch(time.time()),
                            updated_at=_iso_from_epoch(time.time()),
                            parts=[],
                        ).model_dump(exclude_none=True),
                    )
                )
            text_part = Part(
                id=streamed_assistant_part_id,
                type="text",
                text="",
                metadata={"stream_source": "live"},
            )
            live_parts = getattr(app.state, "live_assistant_parts", None)
            if live_parts is None:
                live_parts = {}
                app.state.live_assistant_parts = live_parts
            live_parts.setdefault(sid, []).append(text_part)
            bus.publish(
                Event(
                    type="message.part.added",
                    session_id=sid,
                    payload={
                        "turn_id": turn_id,
                        "message_id": streamed_assistant_msg_id,
                        "stream_source": "live",
                        "part": text_part.model_dump(exclude_none=True),
                    },
                )
            )
        streamed_assistant_buffer.append(text)
        bus.publish(
            Event(
                type="message.part.delta",
                session_id=sid,
                payload={
                    "turn_id": turn_id,
                    "message_id": streamed_assistant_msg_id,
                    "part_id": streamed_assistant_part_id,
                    "stream_source": "live",
                    "delta": {"text_append": text},
                },
            )
        )

    # Unified LM token highway (#693): bind this turn's loop + chat publisher so a
    # blueprint/expert LM call streamed in an executor thread feeds the SAME
    # _emit_chunk — one streaming path for chat AND blueprint turns, instead of
    # the old executor drain-and-discard. The executor inherits this binding via
    # the contextvars.copy_context() at the forward sites below.
    try:
        from clio_agent.runtime.lm_activity import set_live_chunk_emitter  # noqa: PLC0415

        set_live_chunk_emitter(asyncio.get_running_loop(), _emit_chunk)
    except Exception:  # noqa: BLE001 - live-stream wiring is best-effort
        pass

    # iowarp/clio-agent#8: snapshot LM history before the turn so we
    # can sum every call this turn made. ContextVars don't propagate
    # to asyncio executor threads (so dspy.settings.usage_tracker is
    # unreliable from worker threads), but ``lm.history`` IS shared
    # across threads — list.append under the GIL gives us a clean,
    # thread-safe ledger. We diff history[start:end] post-turn.
    history_start = _snapshot_lm_history_index(app)
    _pop_stream_fallback(app, sid)
    turn_cancel_event = threading.Event()
    app.state.cancel_events[sid] = turn_cancel_event
    if sid in app.state.cancel_flags:
        turn_cancel_event.set()
    # No-progress watchdog, not a hard wall: CLIO_GACT_TURN_TIMEOUT_S bounds the
    # gap BETWEEN observable progress events, never the total turn duration. A
    # long-but-progressing turn (a multi-phase EarthScope pipeline: filter ->
    # stage -> profile -> plot, each emitting bus events) must run to completion;
    # only a turn that goes silent for the whole window is wedged and aborted.
    # See [[clio-no-session-timeout]].
    turn_progress_timeout_s = _gact_turn_timeout_s(app)
    # Poll the progress heartbeat on a short cadence so abort latency after the
    # turn truly wedges stays small without busy-waiting. Cap by the window so a
    # tiny configured timeout still polls at least as often.
    _watchdog_poll_s = min(2.0, turn_progress_timeout_s) if turn_progress_timeout_s > 0 else 2.0

    def cancel_requested() -> bool:
        return turn_cancel_event.is_set()

    async def _await_turn_work(awaitable: Any) -> Any:
        if turn_progress_timeout_s <= 0:
            return await awaitable
        # Drive the work as a task and poll for completion. asyncio.wait (unlike
        # wait_for) does NOT cancel the task when the poll interval elapses, so a
        # still-running turn is never disturbed by the watchdog tick. We seed the
        # no-progress clock at "now" so a turn that publishes nothing at all is
        # still bounded by one window; every bus publish for this session (or a
        # global "" event) refreshes it via EventBus.last_publish_monotonic.
        bus = app.state.bus
        task = asyncio.ensure_future(awaitable)
        last_progress = time.monotonic()
        try:
            while True:
                done, _pending = await asyncio.wait({task}, timeout=_watchdog_poll_s)
                if done:
                    return task.result()
                heartbeat = max(
                    bus.last_publish_monotonic(sid),
                    bus.last_publish_monotonic(""),
                )
                if heartbeat > last_progress:
                    last_progress = heartbeat
                # An LM call that is actively generating IS progress, even when it
                # publishes no bus events for the watchdog to see -- a deep-
                # reasoning model streams its chain-of-thought on a separate
                # channel (invisible to DSPy's answer-content listeners) and an
                # expert child runs the call synchronously in an executor (no live
                # deltas at all). Treating an in-flight LM call as progress stops
                # the watchdog from killing a working model mid-think; a per-call
                # ceiling inside lm_call_in_flight() still lets it abort a truly
                # wedged provider. See clio_agent.runtime.lm_activity.
                if _lm_call_in_flight():
                    last_progress = time.monotonic()
                if time.monotonic() - last_progress >= turn_progress_timeout_s:
                    turn_cancel_event.set()
                    task.cancel()
                    try:
                        await task
                    except BaseException:  # noqa: BLE001 - swallow during abort
                        pass
                    raise _TurnTimedOut(turn_progress_timeout_s) from None
        except asyncio.CancelledError:
            # If the work already finished, the cancellation targeted *us* (the
            # watchdog wrapper) after the result was ready -- e.g. event-loop
            # teardown cancelling pending tasks. Surface the completed result
            # rather than masking a finished turn as a cancellation.
            if task.done() and not task.cancelled():
                exc = task.exception()
                if exc is None:
                    return task.result()
            task.cancel()
            raise

    async def _run_dynamic_agent_sync(agent_def: "AgentDef", prompt: str) -> Any:
        runner = _blueprint_runner_for_agent(agent_def)
        loop = asyncio.get_running_loop()
        with _gact_app_context(app), _tool_session_context(sid):
            # The signature is rebuilt inside the executor (via _build_blueprint_dspy_module);
            # its routing Literal[children, "finish"] resolves children from the active
            # blueprint keyed on _ACTIVE_GACT_SESSION_ID. Set it here so the copied context
            # carries it -- otherwise children resolve empty and next_expert collapses to
            # Literal["finish"], forcing the agent to finish immediately.
            _sid_tok = _ctx.set_session_id(sid)
            try:
                turn_context = contextvars.copy_context()
            finally:
                _ctx.reset(_sid_tok)
        _pred = await _await_turn_work(
            loop.run_in_executor(
                None,
                lambda: turn_context.run(
                    _run_dynamic_agent_compat,
                    runner,
                    app.state.agent,
                    agent_def,
                    prompt,
                    sid,
                    cancel_requested,
                ),
            ),
        )
        # RAW-ROUTE instrumentation: what did THIS agent's LM actually emit as
        # structured expert_handoffs (before any continuation-contract injection)?
        # Distinguishes agent-driven routing (model emits handoffs) from
        # contract-driven routing (model emits none; the when_child_completed state
        # machine injects the next expert). Answers whether we can move to the
        # minimal agent-routed loop or whether the blueprint must teach routing.
        trace.route(
            "RAW-ROUTE",
            "agent=%s next_expert=%s next_task_len=%d answer_len=%d",
            getattr(agent_def, "id", "?"),
            str(getattr(_pred, "next_expert", "") or "") or "<none>",
            len(str(getattr(_pred, "next_task", "") or "")),
            len(str(getattr(_pred, "answer", "") or "")),
        )
        # Per-expert capture: one expert.response.completed per dynamic-agent run
        # (child or parent-resume), carrying that expert's full reasoning +
        # trajectory via _prediction_summary. Closes the nested-expert capture
        # gap -- each expert's own LM output is recorded under the turn's trace,
        # correlated by actor agent_id and the parent_expert in blueprint.
        _agent_meta = getattr(agent_def, "metadata", {}) or {}
        _emit_semantic_event(
            app,
            sid,
            "expert.response.completed",
            turn_id=turn_id,
            trace_id=trace_id,
            summary=f"Expert {getattr(agent_def, 'id', '?')} produced a response.",
            actor={"agent_id": str(getattr(agent_def, "id", "") or "")},
            blueprint={
                "agent_blueprint_id": str(_agent_meta.get("agent_blueprint_id") or ""),
                "parent_expert": str(getattr(agent_def, "parent_id", "") or ""),
            },
            provider={
                "provider_id": str(getattr(agent_def, "default_provider", "") or ""),
                "model_id": str(getattr(agent_def, "default_model", "") or ""),
            },
            payload=_prediction_summary(_pred),
        )
        return _pred

    async def _execute_delegated_experts(
        parent_agent: "AgentDef",
        rows: list[dict[str, Any]],
        *,
        source_text: str,
        completed_child_ids: set[str] | None = None,
        completed_child_outputs: dict[str, str] | None = None,
        depth: int = 0,
        seen: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        if seen is None:
            seen = {parent_agent.id}
        completed_child_ids = completed_child_ids or set()
        completed_child_outputs = completed_child_outputs or {}
        if depth >= 3:
            return [
                {
                    **row,
                    "status": "skipped",
                    "skip_reason": "max_delegate_depth_reached",
                    "parent_id": parent_agent.id,
                    "depth": depth,
                }
                for row in rows
                if _should_execute_delegated_handoff(row)
            ]

        executed: list[dict[str, Any]] = []
        for row in rows:
            if not _should_execute_delegated_handoff(row):
                executed.append(row)
                continue
            target_id = _delegated_expert_agent_id(row)
            if not target_id:
                executed.append(
                    {
                        **row,
                        "status": "skipped",
                        "skip_reason": "missing_delegate_target",
                        "parent_id": parent_agent.id,
                        "depth": depth,
                    }
                )
                continue
            target = _resolve_runtime_dynamic_agent(app, target_id, session_id=sid)
            if target is None or target.source != "expert_pack" or not target.enabled:
                executed.append(
                    {
                        **row,
                        "agent_id": target_id,
                        "status": "failed",
                        "error": "delegate_not_available",
                        "parent_id": parent_agent.id,
                        "depth": depth,
                    }
                )
                continue
            if target.parent_id != parent_agent.id:
                executed.append(
                    {
                        **row,
                        "agent_id": target_id,
                        "status": "failed",
                        "error": "delegate_parent_mismatch",
                        "parent_id": parent_agent.id,
                        "target_parent_id": target.parent_id,
                        "depth": depth,
                    }
                )
                continue
            if target.id in seen:
                executed.append(
                    {
                        **row,
                        "agent_id": target_id,
                        "status": "failed",
                        "error": "delegate_cycle_detected",
                        "parent_id": parent_agent.id,
                        "depth": depth,
                    }
                )
                continue

            prompt = _append_session_workflow_state_context(
                app,
                sid,
                _delegated_expert_prompt(row, source_text),
            )
            target_kind = (
                _blueprint_module_kind(target)
                if _agent_definition_uses_blueprint_runtime(target)
                else ""
            )
            execution_mode = (
                f"blueprint_{target_kind}"
                if target_kind
                else ("tool_agent" if target.tools else "prompt_agent")
            )
            is_blueprint_delegation = bool(target_kind)
            delegation_event_prefix = (
                "blueprint.delegation" if is_blueprint_delegation else "delegation"
            )
            delegation_blueprint = {
                "pack_id": str(target.metadata.get("pack_id") or ""),
                "pack_version": str(target.metadata.get("pack_version") or ""),
                "agent_blueprint_id": str(target.metadata.get("agent_blueprint_id") or ""),
                "parent_expert": parent_agent.id,
                "child_expert": target.id,
            }
            started_at = time.perf_counter()
            started_row = {
                **row,
                "agent_id": target.id,
                "parent_id": parent_agent.id,
                "pack_id": str(target.metadata.get("pack_id") or ""),
                "pack_version": str(target.metadata.get("pack_version") or ""),
                "status": "running",
                "stage": "delegate.started",
                "delegation_lifecycle": "sync",
                "depth": depth,
                "execution_mode": execution_mode,
            }
            _emit_semantic_event(
                app,
                sid,
                f"{delegation_event_prefix}.started",
                turn_id=turn_id,
                trace_id=trace_id,
                status="running",
                summary=f"{parent_agent.id} delegated sync work to {target.id}.",
                actor={"agent_id": parent_agent.id, "role": "parent_expert"},
                subject={"agent_id": target.id, "role": "child_expert"},
                blueprint=delegation_blueprint,
                provider={
                    "provider_id": target.default_provider,
                    "model_id": target.default_model,
                },
                payload=started_row,
            )
            _append_live_assistant_part(
                app,
                sid,
                Part(
                    id=f"live_handoff_{uuid.uuid4().hex[:12]}",
                    type="expert_handoff",
                    text=_expert_handoff_summary(started_row),
                    metadata={**started_row, "stream_source": "live"},
                ),
            )
            ledger_start = 0
            ledger = getattr(app.state, "tool_call_ledger", None)
            if isinstance(ledger, dict):
                session_rows = ledger.get(sid)
                if isinstance(session_rows, list):
                    ledger_start = len(session_rows)
            try:
                pred_child = await _run_dynamic_agent_sync(target, prompt)
                duration_ms = int((time.perf_counter() - started_at) * 1000)
                local_output = str(getattr(pred_child, "answer", "") or "").strip()
                local_output = _append_prediction_workflow_state(local_output, pred_child)
                local_tools_called = _extract_tools_called(pred_child)
                local_workflow_state = _workflow_state_from_outputs([local_output])
                for tool_row in local_tools_called:
                    row_state = tool_row.get("workflow_state")
                    if isinstance(row_state, Mapping):
                        _merge_workflow_state_mapping(local_workflow_state, row_state)
                if local_workflow_state:
                    local_state_block = _workflow_state_payload(local_workflow_state)
                    if local_state_block not in local_output:
                        local_output = (
                            f"{local_output.rstrip()}\n\n"
                            f"CLIO local typed workflow state:\n{local_state_block}"
                        )
                local_output_summary = _compact_dynamic_delegation_output(local_output)
                nested: list[dict[str, Any]] = []
                if target.source == "expert_pack":
                    pred_child, nested = await _settle_dynamic_agent_delegations(
                        target,
                        pred_child,
                        source_text=prompt,
                    )
                raw_answer = str(getattr(pred_child, "answer", "") or "").strip()
                output = _append_prediction_workflow_state(raw_answer, pred_child)
                if not nested:
                    child_rows = _coerce_expert_handoff_rows(
                        getattr(pred_child, "expert_handoffs", None)
                    )
                    nested = await _execute_delegated_experts(
                        target,
                        child_rows,
                        source_text=prompt,
                        completed_child_ids=completed_child_ids,
                        completed_child_outputs=completed_child_outputs,
                        depth=depth + 1,
                        seen={*seen, target.id},
                    )
                # STRUCTURAL empty-answer fallback (no prose-keyword scanning): a
                # tool-driven child can produce real tool evidence but an EMPTY prose
                # answer. ONLY when the answer is genuinely empty do we surface the
                # tool-trajectory evidence or the latest nested-child summary -- a
                # non-empty answer IS the agent's deliverable and is left untouched.
                if not raw_answer:
                    fallback = (
                        _tool_agent_empty_answer_fallback(getattr(pred_child, "trajectory", None))
                        or _latest_parent_resumed_output_summary(nested, target.id)
                        or _latest_delegation_output_summary(nested)
                    )
                    if fallback:
                        output = _append_prediction_workflow_state(fallback, pred_child)
                if nested and (
                    _user_agent_bool_param(
                        target,
                        "bubble_child_evidence_on_completion",
                    )
                    or _user_agent_bool_param(
                        target,
                        "return_child_evidence_on_completion",
                    )
                ):
                    declared_target_child_ids = _runtime_declared_child_ids(
                        app,
                        target.id,
                        session_id=sid,
                    )
                    output = (
                        _bubbled_child_evidence_output_summary(
                            nested,
                            target.id,
                            declared_target_child_ids,
                        )
                        or output
                    )
                output = _append_prediction_workflow_state(output, pred_child)
                if nested:
                    output = _append_nested_workflow_state(output, nested)
                child_tools_called = _extract_tools_called(pred_child)
                if isinstance(ledger, dict):
                    session_rows = ledger.get(sid)
                    if isinstance(session_rows, list) and len(session_rows) > ledger_start:
                        child_tools_called = _merge_tool_call_rows(
                            child_tools_called,
                            [
                                dict(row)
                                for row in session_rows[ledger_start:]
                                if isinstance(row, Mapping)
                            ],
                        )
                workflow_state = _workflow_state_from_outputs([prompt, output])
                # Seed from this expert's own authoritative typed emission. The
                # output text can be reassigned to child-evidence summaries that
                # drop the expert's typed workflow_state block, so re-parsing
                # `output` alone can lose the expert's own state sections. Merging
                # local_workflow_state guarantees an expert's typed state bubbles
                # to its parent for continuation routing. Generic for all packs.
                if local_workflow_state:
                    _merge_workflow_state_mapping(workflow_state, local_workflow_state)
                if nested:
                    _merge_workflow_state_mapping(
                        workflow_state,
                        _workflow_state_from_handoff_rows(nested),
                    )
                for tool_row in child_tools_called:
                    row_state = tool_row.get("workflow_state")
                    if isinstance(row_state, Mapping):
                        _merge_workflow_state_mapping(workflow_state, row_state)
                if workflow_state:
                    state_block = _workflow_state_payload(workflow_state)
                    if state_block not in output:
                        output = (
                            f"{output.rstrip()}\n\n"
                            f"CLIO durable typed workflow state:\n{state_block}"
                        )
                output_summary = _compact_dynamic_delegation_output(output)
                completed_row = {
                    **row,
                    "agent_id": target.id,
                    "parent_id": parent_agent.id,
                    "pack_id": str(target.metadata.get("pack_id") or ""),
                    "pack_version": str(target.metadata.get("pack_version") or ""),
                    "provider_id": target.default_provider,
                    "model_id": target.default_model,
                    "fallback_warnings": list(target.validation_errors),
                    "status": "completed",
                    "stage": "delegate.completed",
                    "delegation_lifecycle": "sync",
                    "return_to": parent_agent.id,
                    "return_payload": "compact_result",
                    "depth": depth,
                    "duration_ms": duration_ms,
                    "execution_mode": execution_mode,
                    "input": prompt,
                    "output_summary": output_summary,
                    "return_output_summary": output_summary,
                    "local_output_summary": local_output_summary,
                    "workflow_state": workflow_state,
                    "local_workflow_state": local_workflow_state,
                    "tools_called": child_tools_called,
                    "children": nested,
                }
                # Canonical trace carries the FULL child output (never capped);
                # completed_row keeps only output_summary because it flows into the
                # parent resume prompt + live Part (projections, must stay compact).
                _emit_semantic_event(
                    app,
                    sid,
                    f"{delegation_event_prefix}.completed",
                    turn_id=turn_id,
                    trace_id=trace_id,
                    summary=f"{target.id} returned a compact result to {parent_agent.id}.",
                    actor={"agent_id": target.id, "role": "child_expert"},
                    subject={"agent_id": parent_agent.id, "role": "parent_expert"},
                    blueprint=delegation_blueprint,
                    provider={
                        "provider_id": target.default_provider,
                        "model_id": target.default_model,
                    },
                    payload={**completed_row, "output": output},
                )
                _append_live_assistant_part(
                    app,
                    sid,
                    Part(
                        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
                        type="expert_handoff",
                        text=_expert_handoff_summary(completed_row),
                        metadata={**completed_row, "stream_source": "live"},
                    ),
                )
                executed.append(completed_row)
                resumed_row = {
                    "agent_id": parent_agent.id,
                    "parent_id": parent_agent.parent_id,
                    "dispatch_target": parent_agent.id,
                    "status": "completed",
                    "stage": "parent.resumed",
                    "delegation_lifecycle": "sync",
                    "resumed_from": target.id,
                    "return_payload": "compact_result",
                    "depth": depth,
                    "output_summary": output_summary,
                    "return_output_summary": output_summary,
                    "workflow_state": workflow_state,
                }
                _emit_semantic_event(
                    app,
                    sid,
                    f"{delegation_event_prefix}.parent_resumed",
                    turn_id=turn_id,
                    trace_id=trace_id,
                    summary=f"{parent_agent.id} resumed after {target.id}.",
                    actor={"agent_id": parent_agent.id, "role": "parent_expert"},
                    subject={"agent_id": target.id, "role": "child_expert"},
                    blueprint=delegation_blueprint,
                    payload=resumed_row,
                )
                _append_live_assistant_part(
                    app,
                    sid,
                    Part(
                        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
                        type="expert_handoff",
                        text=_expert_handoff_summary(resumed_row),
                        metadata={**resumed_row, "stream_source": "live"},
                    ),
                )
                executed.append(resumed_row)
            except (_TurnCancelled, _TurnTimedOut):
                raise
            except Exception as exc:  # noqa: BLE001
                child_tools_called = []
                if isinstance(ledger, dict):
                    session_rows = ledger.get(sid)
                    if isinstance(session_rows, list) and len(session_rows) > ledger_start:
                        child_tools_called = _merge_tool_call_rows(
                            child_tools_called,
                            [
                                dict(row)
                                for row in session_rows[ledger_start:]
                                if isinstance(row, Mapping)
                            ],
                        )
                error_name = type(exc).__name__
                error_message = str(exc)
                workflow_state = _failed_child_delegation_workflow_state(
                    prompt=prompt,
                    child_agent_id=target.id,
                    parent_agent_id=parent_agent.id,
                    error=error_name,
                    message=error_message,
                    tools_called=child_tools_called,
                )
                output_summary = _compact_dynamic_delegation_output(
                    _failed_child_delegation_output_summary(
                        child_agent_id=target.id,
                        parent_agent_id=parent_agent.id,
                        error=error_name,
                        message=error_message,
                        workflow_state=workflow_state,
                    )
                )
                failed_row = {
                    **row,
                    "agent_id": target.id,
                    "parent_id": parent_agent.id,
                    "pack_id": str(target.metadata.get("pack_id") or ""),
                    "pack_version": str(target.metadata.get("pack_version") or ""),
                    "provider_id": target.default_provider,
                    "model_id": target.default_model,
                    "fallback_warnings": list(target.validation_errors),
                    "status": "failed",
                    "stage": "delegate.failed",
                    "depth": depth,
                    "duration_ms": int((time.perf_counter() - started_at) * 1000),
                    "execution_mode": execution_mode,
                    "error": error_name,
                    "message": error_message,
                    "output_summary": output_summary,
                    "workflow_state": workflow_state,
                    "tools_called": child_tools_called,
                }
                _emit_semantic_event(
                    app,
                    sid,
                    f"{delegation_event_prefix}.failed",
                    turn_id=turn_id,
                    trace_id=trace_id,
                    status="failed",
                    summary=f"{target.id} failed during sync delegation.",
                    actor={"agent_id": target.id, "role": "child_expert"},
                    subject={"agent_id": parent_agent.id, "role": "parent_expert"},
                    blueprint=delegation_blueprint,
                    provider={
                        "provider_id": target.default_provider,
                        "model_id": target.default_model,
                    },
                    payload=failed_row,
                )
                _append_live_assistant_part(
                    app,
                    sid,
                    Part(
                        id=f"live_handoff_{uuid.uuid4().hex[:12]}",
                        type="expert_handoff",
                        text=_expert_handoff_summary(failed_row),
                        metadata={**failed_row, "stream_source": "live"},
                    ),
                )
                executed.append(failed_row)
        return executed

    async def _settle_dynamic_agent_delegations(
        parent_agent: "AgentDef",
        initial_pred: Any,
        *,
        source_text: str,
    ) -> tuple[Any, list[dict[str, Any]]]:
        """Run sync child delegations and re-enter the parent with compact returns."""

        latest_pred = initial_pred
        if trace.ROUTE_ON:
            import traceback as _tb_enter  # noqa: PLC0415

            _clio_frames = [
                ln.strip().split("\n")[0] for ln in _tb_enter.format_stack() if "gact/app.py" in ln
            ]
            trace.route(
                "SETTLE-ENTER",
                "parent=%s caller=%s",
                parent_agent.id,
                " <- ".join(reversed(_clio_frames[-6:])),
            )
        all_rows: list[dict[str, Any]] = []
        max_rounds = _user_agent_int_param(parent_agent, "max_sync_delegation_rounds", 12)
        max_rounds = max(1, min(max_rounds, 16))
        completed_child_ids: set[str] = set()
        completed_child_outputs: dict[str, str] = {}
        declared_child_ids = _runtime_declared_child_ids(app, parent_agent.id, session_id=sid)

        for _round in range(max_rounds):
            # AGENT-DRIVEN ROUTING. The parent emitted, at the end of its run, a typed
            # ``next_expert``: the ONE child to descend into, or "finish" to return to
            # ITS parent. No contracts, no prose heuristics -- the structured field IS
            # the routing decision (built in _blueprint_runtime_signature as
            # Literal[children + "finish"]). "finish"/missing/unknown-id => this expert
            # is done; finalize with its ``answer``. main's parent is the user, so main's
            # answer on "finish" is the deliverable.
            next_expert = str(getattr(latest_pred, "next_expert", "") or "").strip()
            next_task = str(getattr(latest_pred, "next_task", "") or "").strip()
            if trace.ROUTE_ON:
                trace.route(
                    "SETTLE",
                    "parent=%s round=%d completed=%s next_expert=%s next_task=%r reasoning=%r answer=%r",
                    parent_agent.id,
                    _round,
                    sorted(completed_child_ids),
                    next_expert or "<none>",
                    next_task[:160],
                    str(getattr(latest_pred, "reasoning", "") or "")[:400],
                    str(getattr(latest_pred, "answer", "") or "")[:200],
                )
            if (
                next_expert in ("", "finish", "none", "done", "stop")
                or next_expert not in declared_child_ids
            ):
                break
            requested_rows = [
                {
                    "delegate_to": next_expert,
                    "agent_id": next_expert,
                    "question": next_task or source_text,
                    "status": "requested",
                    "execute": True,
                    "source": "agent_next_expert",
                }
            ]
            # Forward the parent's CURRENT accumulated state (the typed workflow_state it
            # is holding right now, e.g. station_catalog.station_ids produced by an
            # earlier sibling) as the child's parent-evidence. The static `source_text`
            # is the parent's ORIGINAL input, captured before earlier children ran -- so a
            # later child (e.g. the resolver) otherwise never sees the ranked list it is
            # documented to consume, and falls back to inventing candidate ids.
            current_evidence = _append_prediction_workflow_state(
                str(getattr(latest_pred, "answer", "") or ""), latest_pred
            ).strip()
            executed_rows = await _execute_delegated_experts(
                parent_agent,
                requested_rows,
                source_text=current_evidence or source_text,
                completed_child_ids=completed_child_ids,
                completed_child_outputs=completed_child_outputs,
            )
            all_rows.extend(executed_rows)
            completed_this_round = [
                row
                for row in executed_rows
                if row.get("status") == "completed" and row.get("stage") == "delegate.completed"
            ]
            for row in completed_this_round:
                cid = str(row.get("agent_id") or row.get("delegate_to") or "").strip()
                if cid:
                    completed_child_ids.add(cid)
                    completed_child_outputs[cid] = str(
                        row.get("output_summary") or row.get("summary") or ""
                    ).strip()
            if not completed_this_round:
                # Child could not run (unavailable / cycle / error). Stop instead of
                # looping; the parent's current answer carries whatever evidence exists.
                break
            # Re-invoke the parent with the child's returned evidence so IT emits the
            # next route (descend again, or finish).
            resume_prompt = _dynamic_parent_resume_prompt(
                source_text, parent_agent, all_rows, declared_child_ids=declared_child_ids
            )
            latest_pred = await _run_dynamic_agent_sync(parent_agent, resume_prompt)

        final_answer = str(getattr(latest_pred, "answer", "") or "").strip()
        visible_answer = _user_facing_dynamic_evidence_summary(final_answer)
        if visible_answer and visible_answer != final_answer:
            latest_pred = SimpleNamespace(
                answer=visible_answer,
                selected_expert=parent_agent.id,
                routing_rationale="removed retained evidence scaffolding from final dynamic answer",
                next_expert="finish",
                expert_handoffs=[],
            )
        return latest_pred, all_rows

    try:
        if context_file_error is not None:
            raise _ContextFileAccessError(context_file_error)

        if sid in app.state.cancel_flags:
            app.state.cancel_flags.discard(sid)
            raise _TurnCancelled(
                _cancelled_error_info(
                    sid,
                    execution_cancellation="turn_boundary",
                    executor_work_may_continue=False,
                )
            )

        session_agent_id = _session_agent_id(sess)
        active_agent_id = turn_agent_id or session_agent_id
        active_blueprint_root_id = _runtime_active_agent_blueprint_root_id(app, sid)
        active_blueprint_agent_ids = _runtime_active_agent_blueprint_agent_ids(app, sid)
        if (
            not turn_agent_id
            and active_blueprint_root_id
            and active_agent_id in {"", "main", "default"}
        ):
            active_agent_id = active_blueprint_root_id
        routing_mode = getattr(sess, "routing_mode", "auto") or "auto"
        auto_routed_agent = None
        if (
            _keyword_user_agent_routing_enabled()
            and not turn_agent_id
            and not active_blueprint_agent_ids
            and active_agent_id in {"", "main", "default"}
            and routing_mode in {"auto", "experts"}
        ):
            auto_routed_agent = _keyword_routed_user_agent(app, user_text)
            if auto_routed_agent is not None:
                active_agent_id = auto_routed_agent.id
        invocation_agent_id = active_agent_id or "orchestrator"
        _emit_semantic_event(
            app,
            sid,
            "agent.invocation.started",
            turn_id=turn_id,
            trace_id=trace_id,
            status="running",
            summary=f"Invoking {invocation_agent_id}.",
            actor={"agent_id": invocation_agent_id},
            subject={"message_id": user_msg.id},
            payload={
                "routing_mode": routing_mode,
                "session_agent_id": session_agent_id,
                "turn_agent_id": turn_agent_id,
                "active_blueprint_root_id": active_blueprint_root_id,
                "active_blueprint_agent_ids": active_blueprint_agent_ids,
            },
        )
        from clio_agent.agent import cancellation_checker as _cancellation_checker  # noqa: PLC0415

        _refresh_argonne_lm_token(app.state.agent)

        if (
            active_agent_id not in _EXECUTABLE_SESSION_AGENT_IDS
            or active_agent_id in active_blueprint_agent_ids
        ):
            prompt_registry_factory = getattr(app.state, "prompt_registry_for_request", None)
            prompt_registry = (
                prompt_registry_factory(session_id=sid)
                if callable(prompt_registry_factory)
                else None
            )
            dynamic_agent = _resolve_runtime_dynamic_agent(
                app,
                active_agent_id,
                session_id=sid,
                prompt_registry=prompt_registry,
            )
            if dynamic_agent is None:
                raise _UnsupportedSessionAgent(active_agent_id)
            prompt_resolution = dict(dynamic_agent.metadata.get("prompt_resolution") or {})
            dynamic_agent_used = dynamic_agent
            runner = _blueprint_runner_for_agent(dynamic_agent)
            dynamic_kind = (
                _blueprint_module_kind(dynamic_agent)
                if _agent_definition_uses_blueprint_runtime(dynamic_agent)
                else ""
            )
            execution_mode = (
                f"blueprint_{dynamic_kind}"
                if dynamic_kind
                else ("tool_agent" if dynamic_agent.tools else "prompt_agent")
            )
            agent_runtime = _dynamic_agent_runtime_provenance(
                app,
                dynamic_agent,
                execution_mode=execution_mode,
            )
            with _gact_app_context(app):
                session_token = _ctx.set_session_id(sid)
                try:
                    module = (
                        _build_blueprint_dspy_module(app.state.agent, dynamic_agent)
                        if _agent_definition_uses_blueprint_runtime(dynamic_agent)
                        else (
                            _build_tool_user_agent_module(app.state.agent, dynamic_agent)
                            if dynamic_agent.tools
                            else _build_prompt_user_agent_module(app.state.agent, dynamic_agent)
                        )
                    )
                finally:
                    _ctx.reset(session_token)
            llm_actor = {
                "agent_id": dynamic_agent.id,
                "agent_title": dynamic_agent.title,
                "source": dynamic_agent.source,
                "execution_mode": execution_mode,
            }
            llm_subject = {
                "prompt_id": dynamic_agent.prompt_id,
                "prompt_profile": dynamic_agent.prompt_profile,
                "message_id": user_msg.id,
            }
            _emit_semantic_event(
                app,
                sid,
                "llm.request.started",
                turn_id=turn_id,
                trace_id=trace_id,
                status="running",
                summary=f"LLM request started for {dynamic_agent.id}.",
                actor=llm_actor,
                subject=llm_subject,
                blueprint=dict(agent_runtime.get("agent_blueprint") or {}),
                provider=_llm_provider_payload(app, dynamic_agent.id),
                payload={
                    "request_mode": "streamed",
                    "input": enriched_text,
                    "prompt_resolution": prompt_resolution,
                    "agent_runtime": agent_runtime,
                    "native_image_count": len(native_images),
                },
            )
            with _cancellation_checker(cancel_requested), _tool_session_context(sid):
                pred = await _await_turn_work(
                    _try_streamed_forward_compat(
                        app,
                        enriched_text,
                        sid,
                        _emit_chunk,
                        session_mode=getattr(sess, "mode", "chat"),
                        session_edit_mode=getattr(sess, "edit_mode", "diff"),
                        agent_override=module,
                        images=native_images,
                        cancel_requested=cancel_requested,
                    )
                )
            if pred is not None:
                _emit_semantic_event(
                    app,
                    sid,
                    "llm.response.completed",
                    turn_id=turn_id,
                    trace_id=trace_id,
                    summary=f"LLM response completed for {dynamic_agent.id}.",
                    actor=llm_actor,
                    subject=llm_subject,
                    blueprint=dict(agent_runtime.get("agent_blueprint") or {}),
                    provider=_llm_provider_payload(app, dynamic_agent.id),
                    payload=_prediction_summary(pred),
                )
            if pred is None:
                _emit_semantic_event(
                    app,
                    sid,
                    "llm.request.started",
                    turn_id=turn_id,
                    trace_id=trace_id,
                    status="running",
                    summary=f"Synchronous LLM request started for {dynamic_agent.id}.",
                    actor=llm_actor,
                    subject=llm_subject,
                    blueprint=dict(agent_runtime.get("agent_blueprint") or {}),
                    provider=_llm_provider_payload(app, dynamic_agent.id),
                    payload={
                        "request_mode": "sync",
                        "input": enriched_text,
                        "prompt_resolution": prompt_resolution,
                        "agent_runtime": agent_runtime,
                        "native_image_count": len(native_images),
                    },
                )
                with _cancellation_checker(cancel_requested), _tool_session_context(sid):
                    loop = asyncio.get_running_loop()
                    turn_context = contextvars.copy_context()
                    pred = await _await_turn_work(
                        loop.run_in_executor(
                            None,
                            lambda: turn_context.run(
                                _run_dynamic_agent_compat,
                                runner,
                                app.state.agent,
                                dynamic_agent,
                                enriched_text,
                                sid,
                                cancel_requested,
                            ),
                        ),
                    )
                _emit_semantic_event(
                    app,
                    sid,
                    "llm.response.completed",
                    turn_id=turn_id,
                    trace_id=trace_id,
                    summary=f"Synchronous LLM response completed for {dynamic_agent.id}.",
                    actor=llm_actor,
                    subject=llm_subject,
                    blueprint=dict(agent_runtime.get("agent_blueprint") or {}),
                    provider=_llm_provider_payload(app, dynamic_agent.id),
                    payload=_prediction_summary(pred),
                )
        else:
            # Honour the session's routing override. routing_mode "chat"
            # forces the chat path (no /chat prefix needed); "experts"
            # rejects chat/none classifications. Keep the override scoped
            # to this turn context so concurrent sessions do not mutate the
            # shared ClioAgent instance.
            routing_override = routing_mode
            from clio_agent.agent import routing_mode_override as _routing_override  # noqa: PLC0415

            with _routing_override(routing_override), _cancellation_checker(cancel_requested):
                with _tool_session_context(sid):
                    llm_actor = {
                        "agent_id": active_agent_id or "orchestrator",
                        "source": "builtin",
                        "execution_mode": "clio_agent_forward",
                    }
                    llm_subject = {"message_id": user_msg.id}
                    _emit_semantic_event(
                        app,
                        sid,
                        "llm.request.started",
                        turn_id=turn_id,
                        trace_id=trace_id,
                        status="running",
                        summary="LLM request started for CLIO orchestrator.",
                        actor=llm_actor,
                        subject=llm_subject,
                        provider=_llm_provider_payload(app, active_agent_id or "orchestrator"),
                        payload={
                            "request_mode": "streamed",
                            "routing_mode": routing_override,
                            "session_mode": getattr(sess, "mode", "chat"),
                            "edit_mode": getattr(sess, "edit_mode", "diff"),
                            "input": enriched_text,
                            "native_image_count": len(native_images),
                        },
                    )
                    pred = await _await_turn_work(
                        _try_streamed_forward_compat(
                            app,
                            enriched_text,
                            sid,
                            _emit_chunk,
                            session_mode=getattr(sess, "mode", "chat"),
                            session_edit_mode=getattr(sess, "edit_mode", "diff"),
                            images=native_images,
                            cancel_requested=cancel_requested,
                        )
                    )
                    if pred is not None:
                        _emit_semantic_event(
                            app,
                            sid,
                            "llm.response.completed",
                            turn_id=turn_id,
                            trace_id=trace_id,
                            summary="LLM response completed for CLIO orchestrator.",
                            actor=llm_actor,
                            subject=llm_subject,
                            provider=_llm_provider_payload(app, active_agent_id or "orchestrator"),
                            payload=_prediction_summary(pred),
                        )
                    if pred is None:
                        _emit_semantic_event(
                            app,
                            sid,
                            "llm.request.started",
                            turn_id=turn_id,
                            trace_id=trace_id,
                            status="running",
                            summary="Synchronous LLM request started for CLIO orchestrator.",
                            actor=llm_actor,
                            subject=llm_subject,
                            provider=_llm_provider_payload(app, active_agent_id or "orchestrator"),
                            payload={
                                "request_mode": "sync",
                                "routing_mode": routing_override,
                                "session_mode": getattr(sess, "mode", "chat"),
                                "edit_mode": getattr(sess, "edit_mode", "diff"),
                                "input": enriched_text,
                                "native_image_count": len(native_images),
                            },
                        )
                        loop = asyncio.get_running_loop()
                        turn_context = contextvars.copy_context()
                        pred = await _await_turn_work(
                            loop.run_in_executor(
                                None,
                                lambda: turn_context.run(
                                    _agent_forward_compat,
                                    app.state.agent,
                                    enriched_text,
                                    sid,
                                    getattr(sess, "mode", "chat"),
                                    getattr(sess, "edit_mode", "diff"),
                                    cancel_requested,
                                    native_images,
                                ),
                            ),
                        )
                        _emit_semantic_event(
                            app,
                            sid,
                            "llm.response.completed",
                            turn_id=turn_id,
                            trace_id=trace_id,
                            summary="Synchronous LLM response completed for CLIO orchestrator.",
                            actor=llm_actor,
                            subject=llm_subject,
                            provider=_llm_provider_payload(app, active_agent_id or "orchestrator"),
                            payload=_prediction_summary(pred),
                        )
        if dynamic_agent_used is not None and dynamic_agent_used.source == "expert_pack":
            pred, expert_handoffs = await _settle_dynamic_agent_delegations(
                dynamic_agent_used,
                pred,
                source_text=enriched_text,
            )
        _emit_semantic_event(
            app,
            sid,
            "agent.invocation.completed",
            turn_id=turn_id,
            trace_id=trace_id,
            summary=f"{invocation_agent_id} returned a prediction.",
            actor={"agent_id": invocation_agent_id},
            subject={"message_id": user_msg.id},
            payload={
                "selected_expert": getattr(pred, "selected_expert", "") or "",
                "route_source": getattr(pred, "route_source", "") or "",
                "has_answer": bool(getattr(pred, "answer", "") or ""),
                "has_error_info": bool(getattr(pred, "error_info", None)),
            },
        )

        answer_text = getattr(pred, "answer", "")
        selected_agent = getattr(pred, "selected_expert", "") or ""
        rationale = getattr(pred, "routing_rationale", "")
        route_source = getattr(pred, "route_source", "") or ""
        route_reason = getattr(pred, "route_reason", "") or rationale
        if auto_routed_agent is not None:
            selected_agent = selected_agent or auto_routed_agent.id
            keyword_reason = f"Matched registered user agent {auto_routed_agent.id!r} by keyword."
            route_source = "user_agent_keyword"
            rationale = rationale or keyword_reason
            route_reason = keyword_reason
        pred_error_info = _coerce_error_info(getattr(pred, "error_info", None))
        if pred_error_info is not None:
            if pred_error_info.error == "cancelled":
                pred_error_info.details.setdefault("session_id", sid)
            error_info = pred_error_info
            if not error_info.details.get("partial", False):
                answer_text = ""
        ask_user_action = _coerce_ask_user_action(pred)
        if error_info is None and ask_user_action:
            now_iso = datetime.now(timezone.utc).isoformat()
            options = _ask_user_options_from_action(ask_user_action)
            kind_raw = str(ask_user_action.get("kind") or "").strip()
            kind = kind_raw if kind_raw in {"freeform", "choice", "confirmation"} else ""
            if not kind:
                kind = (
                    "choice"
                    if options and not ask_user_action.get("allow_freeform")
                    else "freeform"
                )
            question = UserQuestion(
                id=_new_question_id(),
                session_id=sid,
                prompt=str(ask_user_action["question"]),
                status="pending",
                kind=kind,  # type: ignore[arg-type]
                options=options,
                created_at=now_iso,
                updated_at=now_iso,
                source="orchestrator_action",
                turn_id=user_msg.id,
                attempt_id=retry_attempt_id,
                metadata={
                    **dict(ask_user_action.get("metadata") or {}),
                    "reason": ask_user_action.get("reason", ""),
                    "caller": ask_user_action.get("caller", {}),
                    "resume_on_answer": True,
                    "source_user_message_id": user_msg.id,
                    "source_user_text": user_text,
                    "selected_agent": selected_agent,
                    "route_source": route_source,
                    "route_reason": route_reason,
                },
            )
            app.state.user_questions[question.id] = question
            _emit_semantic_event(
                app,
                sid,
                "user_question.created",
                turn_id=turn_id,
                trace_id=trace_id,
                status="waiting_user",
                summary="Agent requested user input before continuing.",
                actor={"agent_id": selected_agent or invocation_agent_id},
                subject={"question_id": question.id},
                payload=question.model_dump(exclude_none=True),
            )
            updated = app.state.sessions.update(
                sid,
                status="waiting_user",
                message_count=len(app.state.messages.get(sid, [])),
                metadata_patch={"pending_user_question_id": question.id},
            )
            _finalize_context_frame(
                app,
                sid,
                context_frame["id"],
                "",
                "completed",
                error_info=None,
            )
            bus.publish(
                Event(
                    type="user_question.created",
                    session_id=sid,
                    payload=question.model_dump(exclude_none=True),
                )
            )
            bus.publish(
                Event(
                    type="session.status_changed",
                    session_id=sid,
                    payload={
                        "session_id": sid,
                        "status": "waiting_user",
                        "prev_status": "running",
                        "updated_at": updated.updated_at if updated is not None else "",
                        "pending_user_question_id": question.id,
                    },
                )
            )
            if retry_attempt_id:
                _update_retry_attempt(
                    "completed",
                    metadata_patch={
                        "ask_user_question_id": question.id,
                        "stop_reason": "waiting_user",
                    },
                )
            return
        # iowarp/clio-agent#25: data branch reports which execution
        # path it took ("fast" or "expert_loop"). Empty when not
        # populated by ClioAgent.forward (older code paths, non-data
        # branches not yet migrated).
        execution_path = getattr(pred, "execution_path", "") or ""
        tools_called = _extract_tools_called(pred)
        raw_handoffs = getattr(pred, "expert_handoffs", None) or []
        if not expert_handoffs:
            expert_handoffs = _coerce_expert_handoff_rows(raw_handoffs)
        tools_called = _merge_tool_call_rows(
            tools_called,
            _tool_calls_from_handoff_rows(expert_handoffs),
        )
        # Drain the per-session observer ledger so direct-tool short-
        # circuits (HDF5/Parquet/fs experts that bypass ReAct) still
        # report tools_called on the assistant message metadata.
        ledger = getattr(app.state, "tool_call_ledger", None)
        if ledger is not None:
            observed = ledger.pop(sid, [])
            if observed:
                tools_called = _merge_tool_call_rows(tools_called, observed)
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
            # planner + expert + chat calls cleanly. Falls back to
            # ``last entry only`` for older code paths, then to a
            # character-based estimate when the upstream proxy
            # reports zero (some OpenAI-compatible proxies don't
            # populate usage on chunked replies).
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
                        turn_tokens["input"],
                        turn_tokens["output"],
                    )
        if not turn_cost:
            turn_cost = float(getattr(pred, "cost_usd", 0.0) or 0.0)
        proposed_diffs = list(getattr(pred, "file_diffs", None) or [])
        if not proposed_diffs:
            # Dynamic tool agents call fs_propose_edit as a TOOL and never set
            # pred.file_diffs; promote those results so they materialize as
            # file_diff parts + pending /diffs rows (iowarp/clio-agent#674).
            proposed_diffs = _propose_edit_diffs_from_pred(pred)
        nanoagents = list(getattr(pred, "nanoagents_spawned", None) or [])
        for req in getattr(pred, "permissions_requested", None) or []:
            src = (
                req
                if isinstance(req, dict)
                else {
                    "tool_call": getattr(req, "tool_call", {}),
                    "summary": getattr(req, "summary", ""),
                    "id": getattr(req, "id", ""),
                }
            )
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
            _emit_semantic_event(
                app,
                sid,
                "permission.requested",
                turn_id=turn_id,
                trace_id=trace_id,
                status="pending",
                summary="Tool execution requested user permission.",
                actor={"agent_id": selected_agent or invocation_agent_id},
                subject={"permission_id": pid},
                payload=row,
            )
            bus.publish(
                Event(
                    type="permission.requested",
                    session_id=sid,
                    payload=row,
                )
            )
        if sid in app.state.cancel_flags:
            app.state.cancel_flags.discard(sid)
            error_info = _cancelled_error_info(
                sid,
                execution_cancellation="turn_boundary",
                executor_work_may_continue=False,
            )
            answer_text = ""
            tools_called = []
    except _TurnCancelled as exc:
        error_info = exc.error_info
        answer_text = ""
        tools_called = []
    except asyncio.CancelledError:
        error_info = _cancelled_error_info(
            sid,
            execution_cancellation="best_effort",
            executor_work_may_continue=True,
        )
        answer_text = ""
        tools_called = []
    except _StreamingOutputError as exc:
        original = exc.__cause__ or exc
        error_info = ErrorInfo(
            error="provider_error",
            message=str(exc),
            details={
                "original_error": type(original).__name__,
                "partial_output": bool(streamed_assistant_buffer),
                "stream_source": ("live" if streamed_assistant_buffer else "batch"),
            },
            recoverable=True,
        )
        answer_text = "".join(streamed_assistant_buffer)
        tools_called = []
    except _TurnTimedOut as exc:
        partial_output = bool(streamed_assistant_buffer)
        error_info = ErrorInfo(
            error="provider_timeout",
            message=f"agent turn made no progress for {exc.timeout_s:g}s",
            details={
                "session_id": sid,
                "no_progress_timeout_s": exc.timeout_s,
                "timeout_s": exc.timeout_s,
                "partial_output": partial_output,
                "execution_cancellation": "best_effort",
                "executor_work_may_continue": True,
                "recovery_actions": [
                    "retry",
                    "increase_turn_timeout",
                    "reconfigure_provider",
                    "exit",
                ],
            },
            recoverable=True,
        )
        answer_text = "".join(streamed_assistant_buffer)
        tools_called = []
    except _UnsupportedSessionAgent as exc:
        selected_agent = exc.agent_id
        rationale = (
            "Session selected an agent that is registered but not executable "
            "by CLIO's current runtime."
        )
        error_info = ErrorInfo(
            error="not_implemented",
            message=(f"Session agent {exc.agent_id!r} cannot be executed yet."),
            details={
                "agent_id": exc.agent_id,
                "reason": exc.reason,
                "supported_agent_ids": sorted(
                    agent_id for agent_id in _EXECUTABLE_SESSION_AGENT_IDS if agent_id
                ),
                "unsupported_tools": exc.tools,
                "recovery_actions": [
                    "choose_builtin_agent",
                    "remove_custom_agent_tools",
                    "retry",
                    "exit",
                ],
            },
            recoverable=True,
        )
        answer_text = ""
        tools_called = []
    except _ContextFileAccessError as exc:
        error_info = exc.error_info
        answer_text = ""
        tools_called = []
    except Exception as exc:  # noqa: BLE001
        error_info = ErrorInfo(
            error="agent_error",
            message=f"agent.forward raised: {exc}",
            details={"original_error": type(exc).__name__},
            recoverable=True,
        )

    if error_info is None and not answer_text and expert_handoffs:
        answer_text = _fallback_answer_from_delegation(expert_handoffs)

    # Final user-facing text only: correct any fabricated local artifact (csv/png)
    # path the answer presents as produced — whether the synthesizing expert
    # composed a plausible-but-wrong filename or the delegation-fallback text
    # carried a model-requested ``output_path`` that the tool never wrote — by
    # grounding it against the run's verified on-disk artifacts in the merged
    # typed workflow_state. Generic (typed state + filesystem only), applied once
    # on the assembled answer, never on intermediate child rows.
    if answer_text and expert_handoffs:
        answer_text = _ground_fabricated_local_artifact_paths(
            answer_text,
            _workflow_state_from_handoff_rows(expert_handoffs),
        )

    # Build assistant parts — routing_decision (v0.2) first when we
    # got a selected_agent, then optional thinking trace, then the
    # text answer, then any file_diffs.
    if (
        error_info is None
        and not answer_text
        and not thinking_text
        and not proposed_diffs
        and not nanoagents
    ):
        error_info = ErrorInfo(
            error="empty_response",
            message="Agent completed without user-visible output.",
            details={
                "session_id": sid,
                "routing_mode": getattr(sess, "routing_mode", "auto"),
                "selected_agent": selected_agent,
            },
            recoverable=True,
        )

    live_parts_by_session = getattr(app.state, "live_assistant_parts", {}) or {}
    live_assistant_parts = list(live_parts_by_session.get(sid, []))
    final_live_parts = [part for part in live_assistant_parts if part.type == "text"]
    live_routing_agents = {
        p.selected_agent
        for p in final_live_parts
        if p.type == "routing_decision" and p.selected_agent
    }
    live_has_expert_handoff = any(p.type == "expert_handoff" for p in final_live_parts)
    live_tool_calls = {
        p.call_id: p for p in live_assistant_parts if p.type == "tool_call" and p.call_id
    }
    enriched_live_part_ids: set[str] = set()
    for part in live_assistant_parts:
        if part.type != "tool_result" or not part.call_id:
            continue
        call_part = live_tool_calls.get(part.call_id)
        if call_part is None:
            continue
        for row in tools_called:
            if str(row.get("name") or "") != call_part.tool_name:
                continue
            if row.get("args") != call_part.input:
                continue
            if "result" not in row:
                continue
            part.content = [
                Part(
                    id=f"{part.id}_final_text",
                    type="text",
                    text=_tool_result_preview(row.get("result")),
                )
            ]
            enriched_live_part_ids.add(part.id)
            break

    assistant_parts: list[Part] = []
    if selected_agent and selected_agent not in live_routing_agents:
        assistant_parts.append(
            Part(
                id=_new_part_id(),
                type="routing_decision",
                metadata={
                    k: v
                    for k, v in {
                        "route_source": route_source,
                        "route_reason": route_reason,
                    }.items()
                    if v
                },
                selected_agent=selected_agent,
                rationale=rationale,
                confidence=0.0,
                heuristic=False,
                execution_path=execution_path,
            )
        )
    for handoff in [] if live_has_expert_handoff else expert_handoffs:
        assistant_parts.append(
            Part(
                id=_new_part_id(),
                type="expert_handoff",
                metadata=handoff,
                text=_expert_handoff_summary(handoff),
            )
        )
    if thinking_text:
        # iowarp/clio-agent#17: surface DSPy reasoning as a
        # thinking Part so the TUI can collapse + render it
        # gated on capabilities.thinking_blocks.
        assistant_parts.append(Part(id=_new_part_id(), type="thinking", text=thinking_text))
    assistant_parts.extend(final_live_parts)
    if answer_text and streamed_assistant_part_id is None:
        assistant_parts.append(Part(id=_new_part_id(), type="text", text=answer_text))
    for row in proposed_diffs:
        if isinstance(row, dict):
            getf = row.get
        else:

            def getf(k, default=None, _r=row):
                return getattr(_r, k, default)

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
        diff_part = Part(
            id=_new_part_id(),
            type="file_diff",
            path=path,
            unified_diff=udiff,
            new_content=new_content,
            status="pending",
            edit_mode=edit_mode,
            lines_added=lines_added,
            lines_removed=lines_removed,
        )
        assistant_parts.append(diff_part)
        _emit_semantic_event(
            app,
            sid,
            "artifact.proposed",
            turn_id=turn_id,
            trace_id=trace_id,
            summary=f"Agent proposed a file diff for {path}.",
            actor={"agent_id": selected_agent or invocation_agent_id},
            subject={"path": path, "part_id": diff_part.id, "artifact_type": "file_diff"},
            payload={
                "path": path,
                "unified_diff": udiff,
                "new_content": new_content,
                "edit_mode": edit_mode,
                "lines_added": lines_added,
                "lines_removed": lines_removed,
            },
        )

    error_info = _enrich_cancellation_error_info(app, sid, error_info)
    cancelled_turn = error_info is not None and error_info.error == "cancelled"
    if cancelled_turn:
        app.state.cancel_flags.discard(sid)
        ledger = getattr(app.state, "tool_call_ledger", None)
        if ledger is not None:
            ledger.pop(sid, None)

    assistant_metadata: dict[str, Any] = {}
    if turn_agent_id:
        assistant_metadata["agent_override"] = {
            "requested_agent_id": turn_agent_id,
            "session_agent_id": _session_agent_id(sess),
            "effective_agent_id": selected_agent or turn_agent_id,
            "scope": "turn",
        }
    should_report_stream_provenance = bool(answer_text) or error_info is not None
    text_stream_source = (
        ("live" if streamed_assistant_part_id is not None else "batch")
        if should_report_stream_provenance
        else ""
    )
    if text_stream_source:
        assistant_metadata["stream_source"] = text_stream_source
    stream_fallback = _pop_stream_fallback(app, sid)
    if text_stream_source == "batch":
        if not stream_fallback:
            stream_fallback = _stream_fallback_payload("sync_execution_path")
        assistant_metadata["stream_fallback"] = stream_fallback
    if text_stream_source:
        for part in assistant_parts:
            if part.type != "text" or not part.text:
                continue
            part.metadata = {
                **part.metadata,
                "stream_source": text_stream_source,
            }
            if text_stream_source == "batch" and stream_fallback:
                part.metadata["stream_fallback"] = stream_fallback
    if tools_called:
        assistant_metadata["tools_called"] = tools_called
    if expert_handoffs:
        assistant_metadata["expert_handoffs"] = expert_handoffs
    if context_file_provenance["files"]:
        assistant_metadata["context_files"] = context_file_provenance
    if memory_search_metadata:
        assistant_metadata["memory_search"] = memory_search_metadata
    if agent_runtime:
        assistant_metadata["agent_runtime"] = agent_runtime
    if prompt_resolution:
        assistant_metadata["prompt_resolution"] = prompt_resolution
    # Reasoning capture: log the chain-of-thought tokens for EVERY LM call this
    # turn (planner + each expert + chat), extracted from dspy.lm.history. Most
    # stacks discard reasoning_content; we persist (question, reasoning, response)
    # on the assistant message metadata because the reasoning has scientific
    # value for analysing how the model reached its answer. Gated by
    # CLIO_CAPTURE_REASONING (default on); set to 0 to avoid the metadata growth.
    if os.environ.get("CLIO_CAPTURE_REASONING", "").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }:
        try:
            _reasoning_log = _reasoning_records_from_history_slice(history_start, app)
        except Exception:  # noqa: BLE001 - reasoning capture is best-effort, never fail a turn
            _reasoning_log = []
        if _reasoning_log:
            assistant_metadata["reasoning_log"] = _reasoning_log
            trace.event(
                "REASONING",
                "captured %d call(s): %s",
                len(_reasoning_log),
                "; ".join(
                    f"{(r['model'] or '?').split('/')[-1]}={r['reasoning_chars']}c"
                    for r in _reasoning_log
                ),
            )
    # iowarp/clio-agent#6: when streaming actually emitted chunks,
    # reuse its message_id + part_id so the deltas + final
    # message line up. Otherwise mint a fresh id (existing path).
    live_ids = getattr(app.state, "live_assistant_message_ids", {}) or {}
    live_assistant_msg_id = str(live_ids.get(sid) or "")
    asst_id = streamed_assistant_msg_id or live_assistant_msg_id or _new_message_id("asst")
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
                    metadata=p.metadata,
                )
                break
    assistant_msg = Message(
        id=asst_id,
        # Correlate the assistant reply to the user-turn that produced it (#711).
        turn_id=turn_id,
        session_id=sid,
        role="assistant",
        created_at=_iso_from_epoch(time.time()),
        updated_at=_iso_from_epoch(time.time()),
        parts=assistant_parts,
        tokens=Tokens(**turn_tokens),
        cost_usd=turn_cost,
        stop_reason="cancelled" if cancelled_turn else ("error" if error_info else "end_turn"),
        error_info=error_info,
        metadata=assistant_metadata,
    )
    _finalize_context_frame(
        app,
        sid,
        context_frame["id"],
        assistant_msg.id,
        "cancelled" if cancelled_turn else ("error" if error_info else "completed"),
        error_info=error_info,
    )

    # Index file_diff parts so /diffs/apply + /diffs/reject find them.
    bucket = app.state.pending_diffs.setdefault(sid, [])
    for p in assistant_parts:
        if p.type != "file_diff":
            continue
        write_content = (
            p.new_content if p.new_content or p.edit_mode in {"whole", "patch"} else None
        )
        bucket.append(
            {
                "path": p.path,
                "unified_diff": p.unified_diff,
                "new_content": write_content,
                "status": "pending",
                "part_id": p.id,
                "message_id": assistant_msg.id,
            }
        )

    # Materialise nanoagent spawns + publish their lifecycle events.
    for spawn in nanoagents:
        get = (
            spawn.get
            if isinstance(spawn, dict)
            else (lambda k, default=None, _s=spawn: getattr(_s, k, default))
        )
        agent_id = get("agent_id") or get("agent") or "nanoagent"
        spawn_input = get("input") or {}
        answer = get("answer") or ""
        tools_called = get("tools_called") or get("tools") or []
        subsess = app.state.sessions.create(
            workspace_id=sess.workspace_id,
            title=f"{agent_id} subagent",
            parent_session_id=sid,
            agent={"id": str(agent_id), "mode": "subagent"},
            metadata={
                "session_type": "nanoagent",
                "agent_id": str(agent_id),
                "parent_session_id": sid,
                "spawned_by_message_id": assistant_msg.id,
                "spawned_by_agent": selected_agent,
                "tool_count": len(tools_called) if isinstance(tools_called, list) else 0,
            },
        )
        sub_now = time.time()
        sub_user = Message(
            id=_new_message_id("user"),
            session_id=subsess.id,
            role="user",
            created_at=_iso_from_epoch(sub_now),
            updated_at=_iso_from_epoch(sub_now),
            parts=[
                Part(
                    id=_new_part_id(),
                    type="text",
                    text=_format_subagent_input(spawn_input),
                )
            ],
            metadata={
                "subagent_input": spawn_input,
                "parent_session_id": sid,
                "spawned_by_message_id": assistant_msg.id,
            },
        )
        sub_asst = Message(
            id=_new_message_id("asst"),
            session_id=subsess.id,
            role="assistant",
            created_at=_iso_from_epoch(sub_now),
            updated_at=_iso_from_epoch(sub_now),
            parts=[Part(id=_new_part_id(), type="text", text=answer)] if answer else [],
            stop_reason="end_turn",
            metadata={"tools_called": tools_called} if tools_called else {},
        )
        _extend_session_messages(app, subsess.id, [sub_user, sub_asst])
        app.state.sessions.update(subsess.id, message_count=2, status="idle")
        _emit_semantic_event(
            app,
            sid,
            "subagent.started",
            turn_id=turn_id,
            trace_id=trace_id,
            status="running",
            summary=f"Spawned subagent {agent_id}.",
            actor={"agent_id": selected_agent or "orchestrator"},
            subject={"agent_id": str(agent_id), "session_id": subsess.id},
            payload={
                "parent_session_id": sid,
                "child_session_id": subsess.id,
                "agent_id": agent_id,
                "spawned_by_message_id": assistant_msg.id,
            },
        )
        bus.publish(
            Event(
                type="subagent.started",
                session_id=sid,
                payload={
                    "parent_session_id": sid,
                    "child_session_id": subsess.id,
                    "agent_id": agent_id,
                    "spawned_by_message_id": assistant_msg.id,
                },
            )
        )
        _emit_semantic_event(
            app,
            sid,
            "subagent.completed",
            turn_id=turn_id,
            trace_id=trace_id,
            summary=f"Subagent {agent_id} completed.",
            actor={"agent_id": str(agent_id), "session_id": subsess.id},
            subject={"session_id": sid},
            payload={
                "parent_session_id": sid,
                "child_session_id": subsess.id,
                "agent_id": agent_id,
                "duration_ms": float(get("duration_ms", 0.0) or 0.0),
                "tokens": get("tokens") or {},
                "cost_usd": float(get("cost_usd", 0.0) or 0.0),
            },
        )
        bus.publish(
            Event(
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
            )
        )

    # message.created for the assistant message (empty body — parts
    # arrive via subsequent message.part.added/delta events).
    # When real streaming already fired the message.created +
    # message.part.added + N deltas (#6), skip re-issuing them so we
    # don't duplicate.
    if streamed_assistant_msg_id is None and not live_assistant_msg_id:
        bus.publish(
            Event(
                type="message.created",
                session_id=sid,
                payload=Message(
                    id=assistant_msg.id,
                    turn_id=turn_id,
                    session_id=sid,
                    role="assistant",
                    created_at=assistant_msg.created_at,
                    updated_at=assistant_msg.updated_at,
                    parts=[],
                ).model_dump(exclude_none=True),
            )
        )
    # Stream live text parts via message.part.delta. When a turn only has
    # post-hoc text, publish the completed text as a normal part instead
    # of chunking it into synthetic deltas that could be mistaken for live
    # provider tokens.
    for part in assistant_parts:
        if (
            part.metadata.get("stream_source") == "live"
            and part.id not in enriched_live_part_ids
            and part.type != "text"
        ):
            continue
        if part.type == "text" and part.text:
            if part.id == streamed_assistant_part_id:
                # Real streaming already pumped deltas — but those
                # carry raw LM output that includes ChatAdapter format
                # markers ([[ ## answer ## ]] etc). The final ``part.text``
                # is the parsed clean answer; ship it on the completed
                # event so the TUI can replace the buffered text.
                bus.publish(
                    Event(
                        type="message.part.completed",
                        session_id=sid,
                        payload={
                            "turn_id": turn_id,
                            "message_id": assistant_msg.id,
                            "part_id": part.id,
                            "stream_source": "live",
                            "final_text": part.text,
                        },
                    )
                )
                continue
            delivered = part.model_copy(deep=True)
            delivered.metadata = {
                **delivered.metadata,
                "stream_source": "batch",
            }
            if stream_fallback:
                delivered.metadata["stream_fallback"] = stream_fallback
            bus.publish(
                Event(
                    type="message.part.added",
                    session_id=sid,
                    payload={
                        "turn_id": turn_id,
                        "message_id": assistant_msg.id,
                        "stream_source": "batch",
                        "part": delivered.model_dump(exclude_none=True),
                    },
                )
            )
            bus.publish(
                Event(
                    type="message.part.completed",
                    session_id=sid,
                    payload={
                        "turn_id": turn_id,
                        "message_id": assistant_msg.id,
                        "part_id": part.id,
                        "stream_source": "batch",
                        "stream_fallback": stream_fallback,
                        "final_text": part.text,
                    },
                )
            )
        else:
            bus.publish(
                Event(
                    type="message.part.added",
                    session_id=sid,
                    payload={
                        "turn_id": turn_id,
                        "message_id": assistant_msg.id,
                        "stream_source": str(part.metadata.get("stream_source") or "batch"),
                        "part": part.model_dump(exclude_none=True),
                    },
                )
            )
    # Tool lifecycle events are only emitted by the live observer at the
    # execution boundary. Prediction.tools_called remains summary metadata;
    # do not reconstruct started/completed events after the turn, because
    # that makes post-hoc facts look like live tool timing.
    completed_payload: dict[str, Any] = {
        "turn_id": turn_id,
        "message_id": assistant_msg.id,
        "stop_reason": "cancelled" if cancelled_turn else ("error" if error_info else "end_turn"),
        "tokens": dict(turn_tokens),
        "cost_usd": turn_cost,
    }
    if error_info is not None:
        completed_payload["error_info"] = error_info.model_dump(exclude_none=True)
    if assistant_metadata:
        completed_payload["metadata"] = assistant_metadata
    # Embed the full final assistant message in the DURABLE turn.completed so the
    # messages store is derivable from the canonical trace (the trace is the
    # source of truth). final_message is in SENSITIVE_KEYS, so the SSE projection
    # strips it -- the message already streams to clients via message.* events.
    semantic_completed_payload = {
        **completed_payload,
        "final_message": assistant_msg.model_dump(exclude_none=True),
    }
    _emit_semantic_event(
        app,
        sid,
        "turn.completed" if error_info is None else "turn.failed",
        turn_id=turn_id,
        trace_id=trace_id,
        status="completed" if error_info is None else "failed",
        summary=(
            "CLIO turn completed."
            if error_info is None
            else f"CLIO turn failed: {error_info.error}."
        ),
        actor={"agent_id": selected_agent or "orchestrator"},
        subject={"message_id": assistant_msg.id},
        payload=semantic_completed_payload,
    )
    bus.publish(
        Event(
            type="message.completed",
            session_id=sid,
            payload=completed_payload,
        )
    )

    # Persist + settle.
    final_status = "cancelled" if cancelled_turn else ("error" if error_info else "idle")
    retry_status = "cancelled" if cancelled_turn else ("failed" if error_info else "completed")
    _append_session_message(app, sid, assistant_msg)
    getattr(app.state, "live_assistant_message_ids", {}).pop(sid, None)
    getattr(app.state, "live_assistant_parts", {}).pop(sid, None)
    getattr(app.state, "live_assistant_part_keys", {}).pop(sid, None)
    _update_retry_attempt(
        retry_status,
        metadata_patch={
            "executed_user_message_id": user_msg.id,
            "assistant_message_id": assistant_msg.id,
            "stop_reason": completed_payload["stop_reason"],
        },
    )
    app.state.sessions.update(
        sid,
        status=final_status,
        message_count=sess.message_count + 2,
        add_tokens_input=turn_tokens["input"],
        add_tokens_output=turn_tokens["output"],
        add_cost_usd=turn_cost,
    )
    cancellation_status: dict[str, Any] = {}
    if cancelled_turn and error_info is not None:
        cancellation_status = {
            "execution_cancellation": error_info.details.get("execution_cancellation"),
            "executor_work_may_continue": error_info.details.get("executor_work_may_continue"),
            "cancellation_attempt": error_info.details.get("cancellation_attempt", {}),
        }
    bus.publish(
        Event(
            type="session.status_changed",
            session_id=sid,
            payload={
                "session_id": sid,
                "status": final_status,
                "prev_status": "running",
                **cancellation_status,
            },
        )
    )
    # iowarp/clio-agent#20: post_message hook runs AFTER persistence
    # so user audit code sees the settled assistant + can ship to
    # external systems. Errors are swallowed (post_* contract).
    try:
        from clio_agent.runtime.hooks import fire as _fire_hook

        _emit_semantic_event(
            app,
            sid,
            "hook.invocation.started",
            turn_id=turn_id,
            trace_id=trace_id,
            status="running",
            summary="post_message hook dispatch started.",
            actor={"hook": "post_message"},
            subject={"message_id": assistant_msg.id},
            payload={"assistant": assistant_msg.model_dump(exclude_none=True)},
        )
        _fire_hook(
            "post_message",
            sid,
            assistant_msg.model_dump(exclude_none=True),
            hook_scope={
                "session_id": sid,
                "workspace_id": getattr(sess, "workspace_id", ""),
                "blueprint_id": _runtime_active_agent_blueprint_id(app, sid),
            },
        )
        _emit_semantic_event(
            app,
            sid,
            "hook.invocation.completed",
            turn_id=turn_id,
            trace_id=trace_id,
            summary="post_message hook dispatch completed.",
            actor={"hook": "post_message"},
            subject={"message_id": assistant_msg.id},
            payload={},
        )
    except Exception:  # noqa: BLE001
        _emit_semantic_event(
            app,
            sid,
            "hook.invocation.failed",
            turn_id=turn_id,
            trace_id=trace_id,
            status="failed",
            summary="post_message hook dispatch failed and was swallowed by policy.",
            actor={"hook": "post_message"},
            subject={"message_id": assistant_msg.id},
            payload={},
        )
        pass
    if not (
        cancelled_turn
        and error_info is not None
        and error_info.details.get("execution_cancellation") == "best_effort"
    ):
        if app.state.cancel_events.get(sid) is turn_cancel_event:
            app.state.cancel_events.pop(sid, None)


def _current_lm_model_id() -> str:
    """Best-effort: which model is dspy.settings.lm bound to."""
    try:
        import dspy  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return ""
    lm = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
    return getattr(lm, "model", "") if lm else ""


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
            "session_mode": getattr(sess, "mode", "chat"),
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
    app.state.context_frames.setdefault(sid, []).append(frame)
    app.state.bus.publish(Event(type="context.frame.created", session_id=sid, payload=frame))
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
        app.state.bus.publish(Event(type="context.frame.completed", session_id=sid, payload=frame))
        break


def _all_known_lms(app: "FastAPI") -> list[Any]:
    """Return every LM instance the running agent might call —
    ``dspy.settings.lm`` plus the agent's ``_planner_lm`` and any
    expert-bound LMs. Lets the turn handler diff history across
    all of them so planner + expert + chat token counts roll up."""

    lms: list[Any] = []
    try:
        import dspy  # noqa: PLC0415

        main = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
        if main is not None:
            lms.append(main)
    except Exception:  # pragma: no cover
        pass
    agent = getattr(getattr(app, "state", None), "agent", None)
    # Include _main_lm: the agent's primary LM (planner + experts route through it
    # when it is not the global dspy.settings.lm). Missing it under-counts usage
    # AND drops the reasoning trace for the bulk of the turn. Keep the others.
    for attr in ("_main_lm", "_planner_lm", "_router_lm", "router_lm", "_expert_lm", "main_lm"):
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
    call this turn made across planner + experts + chat. Accepts
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


def _entry_reasoning_text(entry: dict[str, Any]) -> str:
    """Pull the reasoning-channel text out of one dspy ``lm.history`` entry.

    DSPy stores reasoning per call in ``entry["outputs"]`` (each output dict may
    carry ``reasoning_content``) and on the raw ``entry["response"]``
    (``choices[i].message.reasoning_content``). Most stacks discard this; we
    surface it because the chain-of-thought has scientific value for analysing
    how a model reached an answer.
    """

    parts: list[str] = []
    outputs = entry.get("outputs")
    if isinstance(outputs, list):
        for out in outputs:
            if isinstance(out, dict):
                rc = out.get("reasoning_content")
                if rc:
                    parts.append(str(rc))
    if not parts:
        response = entry.get("response")
        choices = getattr(response, "choices", None)
        if isinstance(choices, list):
            for choice in choices:
                msg = getattr(choice, "message", None)
                rc = getattr(msg, "reasoning_content", None) if msg is not None else None
                if rc:
                    parts.append(str(rc))
    return "\n".join(p for p in parts if p).strip()


def _entry_response_text(entry: dict[str, Any]) -> str:
    """Pull the answer text out of one dspy ``lm.history`` entry's outputs."""

    outputs = entry.get("outputs")
    texts: list[str] = []
    if isinstance(outputs, list):
        for out in outputs:
            if isinstance(out, str):
                texts.append(out)
            elif isinstance(out, dict) and out.get("text"):
                texts.append(str(out["text"]))
    return "\n".join(t for t in texts if t).strip()


def _entry_prompt_text(entry: dict[str, Any]) -> str:
    """Best-effort: the rendered user/question prompt for one history entry."""

    prompt = entry.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()
    messages = entry.get("messages")
    if isinstance(messages, list):
        # The last user message is the closest thing to "the question" asked.
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                return str(msg.get("content") or "").strip()
    return ""


def _reasoning_records_from_history_slice(
    start: Any, app: Optional["FastAPI"] = None
) -> list[dict[str, Any]]:
    """Collect ``(question, reasoning, response)`` per LM call in the turn's
    history slice -- across planner + every expert + chat -- so the reasoning
    tokens are LOGGED, not discarded. Only entries that actually carried
    reasoning are included (non-reasoning models yield an empty list)."""

    try:
        import dspy  # noqa: PLC0415
    except Exception:  # pragma: no cover
        return []
    if app is not None:
        lms = _all_known_lms(app)
    else:
        lm = getattr(dspy.settings, "lm", None) if hasattr(dspy, "settings") else None
        lms = [lm] if lm else []
    lms = [lm for lm in lms if lm is not None]
    if not lms:
        return []
    snap = {id(lms[0]): start} if isinstance(start, int) else (start or {})
    records: list[dict[str, Any]] = []
    for lm in lms:
        start_idx = snap.get(id(lm), 0)
        history = getattr(lm, "history", None) or []
        for entry in history[start_idx:]:
            if not isinstance(entry, dict):
                continue
            reasoning = _entry_reasoning_text(entry)
            if not reasoning:
                continue
            records.append(
                {
                    "model": entry.get("model") or "",
                    "question": _entry_prompt_text(entry),
                    "reasoning": reasoning,
                    "response": _entry_response_text(entry),
                    "reasoning_chars": len(reasoning),
                    "timestamp": entry.get("timestamp") or "",
                }
            )
    return records


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
    usage = last.get("usage") if isinstance(last, dict) else getattr(last, "usage", None)
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
    # iowarp/clio-agent#8: some OpenAI-compatible proxies don't pass
    # cost_usd through, so the upstream usage dict reports zero. Fall
    # back to a per-token price table keyed by the LM's model id when
    # raw_cost == 0.
    if raw_cost == 0.0:
        model = ""
        if isinstance(last, dict):
            model = last.get("model") or last.get("response", {}).get("model", "") or ""
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


def _estimate_cost_usd(model_id: str, input_tokens: int, output_tokens: int) -> float:
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


def _is_safe_shell_diagnostic(tool_name: str, args: Mapping[str, Any]) -> bool:
    """Return whether a shell_bash call is a read-only local diagnostic.

    Two classes auto-allow:
      1. A small fixed set of read-only diagnostics (date/pwd/whoami/...).
      2. A bounded text-reshaping pipeline over local files: a command built
         only from safe read/transform utilities (cat/head/tail/awk/cut/sed/sort/
         uniq/grep/wc/echo/tr) plus pipes and a single ``>`` redirect, with NO
         destructive verbs (rm/mv/cp/dd/sudo/curl/wget/chmod/chown/mkfifo/&&-rm).
         This is needed so an expert can normalize a malformed staged reference
         CSV (e.g. the EarthScope station catalog whose header carries unit
         sub-columns) into a clean lat/lon CSV for downstream geo ranking. The
         shell subprocess still runs under the file-policy cwd; this only governs
         whether the call needs interactive approval. Pipelines that touch any
         destructive token fall through to the normal permission gate.
    """

    if tool_name != "shell_bash":
        return False
    command = str(args.get("command") or "").strip()
    normalized = re.sub(r"\s+", " ", command).lower()
    if normalized in {"date", "get-date", "pwd", "whoami", "hostname"}:
        return True
    return _is_safe_text_reshape_command(command) or _is_safe_readonly_diagnostic(command)


# Destructive shell tokens that disqualify a command from the text-reshape
# fast-allow path. Anything here forces the normal interactive permission gate.
_UNSAFE_SHELL_TOKENS: tuple[str, ...] = (
    "rm",
    "rmdir",
    "mv",
    "cp",
    "dd",
    "sudo",
    "su",
    "chmod",
    "chown",
    "chgrp",
    "ln",
    "mkfifo",
    "mknod",
    "curl",
    "wget",
    "scp",
    "rsync",
    "ssh",
    "nc",
    "ncat",
    "telnet",
    "kill",
    "pkill",
    "killall",
    "shutdown",
    "reboot",
    "mkdir",
    "touch",
    "truncate",
    "tee",
    "xargs",
    "find",
    "eval",
    "exec",
    "source",
    "python",
    "python3",
    "perl",
    "ruby",
    "node",
    "bash",
    "sh",
    "zsh",
    "git",
    "apt",
    "pip",
    "uv",
    "npm",
    "yum",
    "brew",
    "systemctl",
    "service",
    "crontab",
    "at",
    "export",
    "unset",
    "alias",
    "function",
)
# Utilities permitted in a text-reshape pipeline.
_SAFE_RESHAPE_UTILS: frozenset[str] = frozenset(
    {
        "cat",
        "head",
        "tail",
        "awk",
        "gawk",
        "cut",
        "sed",
        "sort",
        "uniq",
        "grep",
        "egrep",
        "fgrep",
        "wc",
        "echo",
        "tr",
        "paste",
        "column",
        "nl",
        "printf",
        "true",
    }
)

# Read-only inspection utilities (no writes). Superset of the reshape utils plus
# pure file/dir inspectors. Used to auto-allow harmless diagnostic chains so a
# model's `ls -la X && head -5 X` is not routed to an interactive approval gate
# that would hang in a headless/autonomous run.
_SAFE_READONLY_UTILS: frozenset[str] = _SAFE_RESHAPE_UTILS | frozenset(
    {"ls", "stat", "file", "du", "df", "realpath", "basename", "dirname", "test", "[", "od", "xxd"}
)


def _is_safe_readonly_diagnostic(command: str) -> bool:
    """Return whether a shell_bash command is a bounded READ-ONLY inspection chain.

    Allows commands built ONLY from read-only utilities joined by ``&&`` / ``;`` /
    ``|``, with NO output redirect, NO command/process substitution, and NO
    background or destructive token. This lets an expert inspect staged files
    (``ls -la /tmp/x.csv && head -5 /tmp/x.csv``) without falling through to the
    interactive permission gate — which has no approver in headless/test runs and
    therefore hangs. It writes nothing, so it cannot mutate state.
    """

    if not command or len(command) > 2000:
        return False
    # No command/process substitution, no writes/appends.
    if any(tok in command for tok in ("`", "$(", "<(", ">(", ">>", ">")):
        return False
    # Allow `&&` as a separator but reject a bare background `&`.
    if "&" in command.replace("&&", ""):
        return False
    # No destructive verb anywhere in the command.
    words = re.findall(r"[a-z0-9_./-]+", command.lower())
    if any(w.split("/")[-1] in _UNSAFE_SHELL_TOKENS for w in words):
        return False
    # Every segment (split on && ; |) must start with a read-only utility.
    for seg in re.split(r"&&|;|\|", command):
        seg = seg.strip()
        if not seg:
            continue
        m = re.match(r"([A-Za-z0-9_./\[-]+)", seg)
        if not m:
            return False
        first = m.group(1).split("/")[-1].lower()
        if first not in _SAFE_READONLY_UTILS:
            return False
    return True


def _is_safe_text_reshape_command(command: str) -> bool:
    """Heuristic: a bounded read/transform pipeline that WRITES its output to a
    derived file under ``/tmp/`` with no destructive tokens. Conservative — any
    unrecognized leading word, destructive token, or a non-/tmp/missing output
    target rejects, so the call falls through to the normal permission gate. A
    bare read (e.g. ``cat pyproject.toml`` with no redirect) is NOT auto-allowed;
    only the reshape-and-write-to-/tmp use case is."""

    if not command or len(command) > 2000:
        return False
    # Reject backticks / command substitution / process substitution / append
    # (``>>``) / background (``&``) / chaining (``||``).
    if any(tok in command for tok in ("`", "$(", "<(", ">(", ">>", "&", "||")):
        return False
    # Must redirect output to a single /tmp/ file (the reshape target). Reject a
    # bare read with no redirect, or a redirect to anywhere outside /tmp/.
    redirects = re.findall(r">\s*(\S+)", command)
    if len(redirects) != 1 or not redirects[0].strip("'\"").startswith("/tmp/"):
        return False
    # Tokenize on whitespace and pipes; check no destructive token appears and
    # every "leading" utility (start of a pipe segment) is in the safe set.
    lowered = command.lower()
    words = re.findall(r"[a-z0-9_./-]+", lowered)
    for w in words:
        base = w.split("/")[-1]
        if base in _UNSAFE_SHELL_TOKENS:
            return False
    # Each pipe segment must start with a safe utility or a brace group.
    segments = re.split(r"\|", command)
    for seg in segments:
        seg = seg.strip().lstrip("{").strip()
        if not seg:
            continue
        m = re.match(r"([A-Za-z0-9_./-]+)", seg)
        if not m:
            return False
        first = m.group(1).split("/")[-1].lower()
        # Allow a brace-group inner statement starting with echo/printf etc.
        if first == "}":
            continue
        if first not in _SAFE_RESHAPE_UTILS:
            return False
    return True


def _policy_action_for_tool(
    app: "FastAPI",
    *,
    session_id: str,
    session: Any | None,
    tool_name: str,
    args: Mapping[str, Any],
) -> str:
    """Return the first matching permission policy action.

    The `/v1/policies` endpoint is user-facing configuration, so storing
    policies without enforcing them is a silent safety bypass. Matching is
    intentionally small and predictable: scope, tool glob, optional path glob,
    then the policy action.
    """

    policies = getattr(app.state, "permission_policies", [])
    if not isinstance(policies, list):
        return ""
    path = _permission_path_from_args(args)
    workspace_id = getattr(session, "workspace_id", "") if session is not None else ""
    for policy in policies:
        if not isinstance(policy, dict):
            continue
        scope = str(policy.get("scope") or "").lower()
        scope_id = str(policy.get("scope_id") or "")
        if scope == "session":
            if scope_id and scope_id != session_id:
                continue
        elif scope == "workspace":
            if scope_id and scope_id != workspace_id:
                continue
        else:
            continue

        tool_pattern = str(policy.get("tool_name_pattern") or "*")
        if not fnmatch.fnmatchcase(tool_name, tool_pattern):
            continue

        path_pattern = str(policy.get("path_pattern") or "")
        if path_pattern:
            candidates = [path]
            if path:
                try:
                    candidates.append(str(Path(path).resolve(strict=False)))
                except OSError:
                    pass
            if not any(fnmatch.fnmatchcase(candidate, path_pattern) for candidate in candidates):
                continue

        action = str(policy.get("action") or "").lower()
        if action in {"allow", "allow_session", "allow_workspace", "deny", "ask"}:
            return action
    return ""


def _record_resolved_permission(
    app: "FastAPI",
    *,
    session_id: str,
    tool_name: str,
    args: Mapping[str, Any],
    status: str,
    action: str,
    summary: str,
    reason: str,
) -> str:
    pid = f"perm_{uuid.uuid4().hex[:12]}"
    now_iso = datetime.now(timezone.utc).isoformat()
    row = {
        "id": pid,
        "session_id": session_id,
        "tool_call": {
            "tool_name": tool_name,
            "input": dict(args),
        },
        "summary": summary,
        "created_at": now_iso,
        "status": status,
        "action": action,
        "resolved_at": now_iso,
        "reason": reason,
    }
    if hasattr(app.state, "permissions"):
        app.state.permissions[pid] = row
    if hasattr(app.state, "bus"):
        app.state.bus.publish(
            Event(
                type="permission.resolved",
                session_id=session_id,
                payload={
                    "permission_id": pid,
                    "action": action,
                    "session_id": session_id,
                    "reason": reason,
                },
            )
        )
    return pid


def _direct_permission_denied(
    *,
    tool_name: str,
    args: Mapping[str, Any],
    summary: str,
) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="permission_error",
                message=f"{summary} blocked by permission policy",
                details={
                    "tool_name": tool_name,
                    "input": dict(args),
                    "reason": "policy_deny",
                    "recovery_actions": ["change_policy", "retry", "exit"],
                },
                recoverable=True,
            )
        ).model_dump(exclude_none=True),
    )


def _guard_direct_destructive_action(
    app: "FastAPI",
    *,
    session_id: str = "",
    workspace_id: str = "",
    tool_name: str,
    args: Mapping[str, Any],
    summary: str,
    reason: str,
) -> None:
    """Apply permission policy/audit semantics to direct GACT DELETE actions.

    These routes are already explicit user actions, so there is no extra
    interactive prompt. Policies can still deny before mutation, and all
    allowed direct destructive actions land in `/v1/permissions` as resolved
    audit rows.
    """

    session = app.state.sessions.get(session_id) if session_id else None
    if session is None and workspace_id:
        session = SimpleNamespace(workspace_id=workspace_id)
    policy_action = _policy_action_for_tool(
        app,
        session_id=session_id,
        session=session,
        tool_name=tool_name,
        args=args,
    )
    if policy_action == "deny":
        _record_resolved_permission(
            app,
            session_id=session_id,
            tool_name=tool_name,
            args=args,
            status="auto_denied",
            action="deny",
            summary=f"{summary} blocked by permission policy",
            reason="policy_deny",
        )
        raise _direct_permission_denied(tool_name=tool_name, args=args, summary=summary)
    _record_resolved_permission(
        app,
        session_id=session_id,
        tool_name=tool_name,
        args=args,
        status="auto_approved",
        action="allow",
        summary=summary,
        reason="policy_allow"
        if policy_action in {"allow", "allow_session", "allow_workspace"}
        else reason,
    )


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
        # Prefer the session currently driving the turn. Recency is
        # only a fallback for truly out-of-band tool calls.
        sid, current = _resolve_tool_session(app)
        if current is not None:
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
                        f"destructive tool {name!r} blocked by session.mode={current.mode!r}"
                    ),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "status": "auto_denied",
                    "action": "deny",
                    "resolved_at": datetime.now(timezone.utc).isoformat(),
                }
                app.state.permissions[row["id"]] = row
                app.state.bus.publish(
                    Event(
                        type="permission.resolved",
                        session_id=sid,
                        payload={
                            "permission_id": row["id"],
                            "action": "deny",
                            "session_id": sid,
                            "reason": "session_mode_readonly",
                        },
                    )
                )
                return "deny"
        policy_action = _policy_action_for_tool(
            app,
            session_id=sid,
            session=current,
            tool_name=name,
            args=args,
        )
        if policy_action == "deny":
            _record_resolved_permission(
                app,
                session_id=sid,
                tool_name=name,
                args=args,
                status="auto_denied",
                action="deny",
                summary=f"destructive tool {name!r} blocked by permission policy",
                reason="policy_deny",
            )
            return "deny"
        if policy_action in {"allow", "allow_session", "allow_workspace"}:
            _record_resolved_permission(
                app,
                session_id=sid,
                tool_name=name,
                args=args,
                status="auto_approved",
                action="allow",
                summary=f"destructive tool {name!r} allowed by permission policy",
                reason=f"policy_{policy_action}",
            )
            return "allow"
        if _is_safe_shell_diagnostic(name, args):
            return "allow"
        if not sid:
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
        app.state.bus.publish(
            Event(
                type="permission.requested",
                session_id=sid,
                payload=row,
            )
        )
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


def _make_cancellation_checker(app: "FastAPI"):
    """Build a tool-executor cancellation checker for the active GACT session."""

    def check() -> bool:
        sid, _current = _resolve_tool_session(app)
        if not sid:
            return False
        event = app.state.cancel_events.get(sid)
        if event is not None and event.is_set():
            return True
        return sid in app.state.cancel_flags

    return check


def _install_tool_runtime_hooks(app: "FastAPI") -> None:
    """Install permission, cancellation, and telemetry hooks for tool calls."""

    from clio_agent.tools.execution import (  # noqa: PLC0415
        set_global_cancellation_checker,
        set_global_permission_gate,
        set_global_tool_interceptor,
        set_global_tool_observer,
    )

    checker = getattr(app.state, "pending_cancellation_checker", None)
    if checker is None:
        checker = _make_cancellation_checker(app)
    gate = getattr(app.state, "pending_permission_gate", None)
    if gate is None:
        gate = _make_permission_gate(app)
    observer = getattr(app.state, "pending_tool_observer", None)
    if observer is None:
        observer = _make_tool_observer(app)
    interceptor = getattr(app.state, "pending_tool_interceptor", None)
    set_global_cancellation_checker(checker)
    set_global_permission_gate(gate)
    set_global_tool_interceptor(interceptor)
    set_global_tool_observer(observer)
    app.state.pending_cancellation_checker = checker
    app.state.pending_permission_gate = gate
    app.state.pending_tool_interceptor = interceptor
    app.state.pending_tool_observer = observer
    app.state.tool_hooks_installed = True


def _ensure_live_assistant_message(app: "FastAPI", sid: str) -> str:
    """Return the in-flight assistant message id, creating it if needed."""

    live_ids = getattr(app.state, "live_assistant_message_ids", None)
    if live_ids is None:
        live_ids = {}
        app.state.live_assistant_message_ids = live_ids
    msg_id = str(live_ids.get(sid) or "")
    if msg_id:
        return msg_id
    msg_id = _new_message_id("asst")
    live_ids[sid] = msg_id
    now = _iso_from_epoch(time.time())
    app.state.bus.publish(
        Event(
            type="message.created",
            session_id=sid,
            payload=Message(
                id=msg_id,
                turn_id=_active_semantic_turn_id(),
                session_id=sid,
                role="assistant",
                created_at=now,
                updated_at=now,
                parts=[],
            ).model_dump(exclude_none=True),
        )
    )
    return msg_id


def _append_live_assistant_part(app: "FastAPI", sid: str, part: Part) -> None:
    """Publish and remember a real runtime part for the active assistant turn."""

    msg_id = _ensure_live_assistant_message(app, sid)
    live_parts = getattr(app.state, "live_assistant_parts", None)
    if live_parts is None:
        live_parts = {}
        app.state.live_assistant_parts = live_parts
    live_parts.setdefault(sid, []).append(part)
    app.state.bus.publish(
        Event(
            type="message.part.added",
            session_id=sid,
            payload={
                "turn_id": _active_semantic_turn_id(),
                "message_id": msg_id,
                # Real runtime parts (tool calls/results, routing) emitted live during
                # the turn (#711); not provider-token text, but emitted in real time.
                "stream_source": str(part.metadata.get("stream_source") or "live"),
                "part": part.model_dump(exclude_none=True),
            },
        )
    )


def _append_live_assistant_part_once(
    app: "FastAPI",
    sid: str,
    key: str,
    part: Part,
) -> bool:
    """Publish a live part once per in-flight turn.

    Tool observers can fire many times for the same routed expert. The
    transcript should show the route decision once, then the concrete tool
    calls/results under it, not repeat the same route banner for every call.
    """

    live_keys = getattr(app.state, "live_assistant_part_keys", None)
    if live_keys is None:
        live_keys = {}
        app.state.live_assistant_part_keys = live_keys
    session_keys = live_keys.setdefault(sid, set())
    if key in session_keys:
        return False
    session_keys.add(key)
    _append_live_assistant_part(app, sid, part)
    return True


def _agent_tool_owner(app: "FastAPI", tool_name: str) -> tuple[str, str]:
    """Return (public_parent, owner) for a tool if CLIO can resolve it."""

    agent = getattr(app.state, "agent", None)
    if agent is None:
        return "", ""
    candidates = [tool_name]
    if "." in tool_name:
        candidates.append(tool_name.rsplit(".", 1)[-1])
    for candidate in candidates:
        try:
            owner = str(agent._selected_expert_for_tool(candidate) or "")  # noqa: SLF001
        except Exception:  # noqa: BLE001
            continue
        if not owner:
            continue
        try:
            parent = str(agent._parent_route_for_child(owner) or "")  # noqa: SLF001
        except Exception:  # noqa: BLE001
            parent = ""
        return parent or owner, owner
    return "", ""


def _emit_live_tool_route_context(app: "FastAPI", sid: str, tool_name: str) -> None:
    """Emit route/handoff context immediately before a live tool call."""

    public_agent, owner = _agent_tool_owner(app, tool_name)
    if not public_agent or public_agent in {"chat", "none"}:
        return
    _append_live_assistant_part_once(
        app,
        sid,
        f"route:{public_agent}",
        Part(
            id=f"live_route_{public_agent}",
            type="routing_decision",
            selected_agent=public_agent,
            rationale=f"Agent planner selected {public_agent} for tool {tool_name}.",
            confidence=0.0,
            heuristic=False,
            metadata={
                "route_source": "live_tool_observer",
                "route_reason": f"Resolved from live tool owner {owner}.",
                "stream_source": "live",
            },
            execution_path=f"orchestrator -> {public_agent}",
        ),
    )
    if owner and owner != public_agent:
        row = {
            "agent_id": owner,
            "parent_id": public_agent,
            "dispatch_target": owner,
            "status": "running",
            "stage": "tool.started",
            "delegation_lifecycle": "sync",
            "execution_mode": "tool",
            "depth": 1,
            "output_summary": f"Preparing {tool_name}.",
        }
        _append_live_assistant_part_once(
            app,
            sid,
            f"handoff:{public_agent}:{owner}",
            Part(
                id=f"live_handoff_{public_agent}_{owner}",
                type="expert_handoff",
                text=_expert_handoff_summary(row),
                metadata={**row, "stream_source": "live", "route_source": "live_tool_observer"},
            ),
        )


def _make_tool_observer(app: "FastAPI"):
    """Build a callable suitable for MCPToolBridge.tool_observer.

    Publishes tool.call.started / tool.call.completed events into
    the EventBus, attaching to the active turn session when present
    and falling back to recency only for out-of-band calls. Also
    appends each completed call into ``app.state.tool_call_ledger[sid]`` so the
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
        result: Any | None = None,
    ) -> None:
        sid, _current = _resolve_tool_session(app)
        if not sid:
            return
        if phase == "started":
            call_id = f"call_{uuid.uuid4().hex[:12]}"
            # Stash the per-thread call_id so the completion event
            # uses the same id. Threading-locals works for
            # MCPToolBridge's worker thread.
            _OBSERVER_CALL_IDS.value = call_id
            # Stamp the start time so completion can compute duration.
            _OBSERVER_CALL_T0.value = time.time()
            _emit_live_tool_route_context(app, sid, name)
            _emit_semantic_event(
                app,
                sid,
                "tool.call.started",
                turn_id=_ctx.active_turn_id(),
                trace_id=_ctx.active_trace_id(),
                status="running",
                summary=f"Tool {name} started.",
                actor={"tool": name},
                subject={"call_id": call_id},
                payload={
                    "call_id": call_id,
                    "tool": name,
                    "args": dict(args),
                    "telemetry_source": "live_observer",
                },
            )
            app.state.bus.publish(
                Event(
                    type="tool.call.started",
                    session_id=sid,
                    payload={
                        "call_id": call_id,
                        "tool": name,
                        "args": dict(args),
                        "telemetry_source": "live_observer",
                    },
                )
            )
            _append_live_assistant_part(
                app,
                sid,
                Part(
                    id=f"live_{call_id}_call",
                    type="tool_call",
                    call_id=call_id,
                    tool_name=name,
                    input=dict(args),
                    metadata={"stream_source": "live", "telemetry_source": "live_observer"},
                ),
            )
        elif phase == "completed":
            call_id = getattr(_OBSERVER_CALL_IDS, "value", "") or ""
            t0 = getattr(_OBSERVER_CALL_T0, "value", None)
            duration_ms = (time.time() - t0) * 1000 if t0 else 0.0
            cancel_event = app.state.cancel_events.get(sid)
            completed_after_cancel = sid in app.state.cancel_flags or (
                cancel_event is not None and cancel_event.is_set()
            )
            completion_error = error
            cancellation_metadata: dict[str, Any] = {}
            if completed_after_cancel:
                completion_error = (
                    completion_error or "tool call completed after session cancellation"
                )
                cancellation_metadata = {
                    "execution_cancellation": "best_effort",
                    "executor_work_may_continue": True,
                }
            ok = completion_error is None
            result_summary = f"Tool {name} {'completed' if ok else 'failed'}."
            payload = {
                "call_id": call_id,
                "tool": name,
                "ok": ok,
                "duration_ms": duration_ms,
                "cached": False,
                "telemetry_source": "live_observer",
                "ui_summary": result_summary,
                "result_summary": result_summary,
                **({"error": completion_error} if completion_error else {}),
                **({"result": _bounded_tool_call_result(result)} if result is not None else {}),
                **cancellation_metadata,
            }
            # Append to the per-session ledger FIRST -- before the (potentially
            # I/O-bound, e.g. durable-trace-writing) semantic emit + live parts --
            # so the turn handler's post-forward drain never races a slow emit and
            # drops tools_called from the assistant message metadata.
            ledger = getattr(app.state, "tool_call_ledger", None)
            if ledger is not None and not completed_after_cancel:
                ledger.setdefault(sid, []).append(
                    {
                        "name": name,
                        "call_id": call_id,
                        "args": dict(args),
                        "ok": ok,
                        "duration_ms": duration_ms,
                        "cached": False,
                        "telemetry_source": "live_observer",
                        **({"error": completion_error} if completion_error else {}),
                        **(
                            {"result": _bounded_tool_call_result(result)}
                            if result is not None
                            else {}
                        ),
                        **cancellation_metadata,
                    }
                )
            # Canonical trace captures the FULL tool result (never capped) -- the
            # bounded projection in `payload` is only for the wire bus event +
            # ledger/assistant-metadata. (SSE still redacts `result` via
            # SENSITIVE_KEYS; only the durable trace keeps the full value.)
            trace_payload = {**payload, "result": result} if result is not None else payload
            _emit_semantic_event(
                app,
                sid,
                "tool.call.completed",
                turn_id=_ctx.active_turn_id(),
                trace_id=_ctx.active_trace_id(),
                status="completed" if ok else "failed",
                summary=result_summary,
                actor={"tool": name},
                subject={"call_id": call_id},
                payload=trace_payload,
            )
            app.state.bus.publish(
                Event(
                    type="tool.call.completed",
                    session_id=sid,
                    payload=payload,
                )
            )
            result_text = completion_error or (
                _tool_result_preview(result) if result is not None else "completed"
            )
            _append_live_assistant_part(
                app,
                sid,
                Part(
                    id=f"live_{call_id}_result",
                    type="tool_result",
                    call_id=call_id,
                    tool_name=name,
                    is_error=not ok,
                    duration_ms=duration_ms,
                    cached=False,
                    content=[
                        Part(
                            id=f"live_{call_id}_result_text",
                            type="text",
                            text=result_text,
                        )
                    ],
                    metadata={
                        "stream_source": "live",
                        "telemetry_source": "live_observer",
                        **(
                            {"result": _bounded_tool_call_result(result)}
                            if result is not None
                            else {}
                        ),
                        **cancellation_metadata,
                    },
                ),
            )

    return observe


_OBSERVER_CALL_T0 = threading.local()


def _agent_forward_compat(
    agent: Any,
    question: str,
    session_id: str,
    session_mode: str,
    session_edit_mode: str,
    cancel_requested: Any | None = None,
    images: list[Any] | None = None,
) -> Any:
    """Call agent.forward, threading session_mode + session_edit_mode
    when the agent accepts them, falling back to the legacy
    ``(question, session_id)`` signature for fakes / older builds.

    Lets us add new optional kwargs to the contract without breaking
    every test fixture that hand-rolled a minimal forward signature.
    """

    optional_kwargs: dict[str, Any] = {
        "images": images or [],
        "cancel_requested": cancel_requested,
    }
    attempts = [
        optional_kwargs,
        {"cancel_requested": cancel_requested},
        {"images": images or []},
        {},
    ]
    last_type_error: TypeError | None = None
    for optional in attempts:
        try:
            return agent.forward(
                question,
                session_id=session_id,
                session_mode=session_mode,
                session_edit_mode=session_edit_mode,
                **optional,
            )
        except TypeError as exc:
            message = str(exc)
            if "images" not in message and "cancel_requested" not in message:
                last_type_error = exc
                break
            last_type_error = exc

    try:
        return agent.forward(question, session_id=session_id)
    except TypeError as exc:
        if last_type_error is not None:
            raise last_type_error from exc
        raise


async def _try_streamed_forward_compat(
    app: "FastAPI",
    enriched_text: str,
    sid: str,
    emit_chunk: Any,
    *,
    session_mode: str = "chat",
    session_edit_mode: str = "diff",
    images: list[Any] | None = None,
    agent_override: Any | None = None,
    cancel_requested: Any | None = None,
) -> Optional[Any]:
    """Call _try_streamed_forward with a legacy-signature fallback for tests/plugins."""

    base_kwargs: dict[str, Any] = {
        "session_mode": session_mode,
        "session_edit_mode": session_edit_mode,
    }
    if agent_override is not None:
        base_kwargs["agent_override"] = agent_override

    optional_attempts: list[dict[str, Any]] = [
        {"images": images or [], "cancel_requested": cancel_requested},
        {"cancel_requested": cancel_requested},
        {"images": images or []},
        {},
    ]
    last_type_error: TypeError | None = None
    for optional in optional_attempts:
        try:
            return await _try_streamed_forward(
                app,
                enriched_text,
                sid,
                emit_chunk,
                **base_kwargs,
                **optional,
            )
        except TypeError as exc:
            message = str(exc)
            if "cancel_requested" not in message and "images" not in message:
                raise
            last_type_error = exc
    if last_type_error is not None:
        raise last_type_error
    return None


def _run_dynamic_agent_compat(
    runner: Any,
    base_agent: Any,
    dynamic_agent: Any,
    question: str,
    sid: str,
    cancel_requested: Any | None,
) -> Any:
    """Run a dynamic agent while preserving older runner call signatures."""

    try:
        return runner(base_agent, dynamic_agent, question, sid, cancel_requested)
    except TypeError as exc:
        if "positional" not in str(exc) and "argument" not in str(exc):
            raise
        return runner(base_agent, dynamic_agent, question, sid)


_OBSERVER_CALL_IDS = threading.local()


class _StreamingOutputError(RuntimeError):
    """Raised when live streaming fails after user-visible output was emitted."""


def _stream_fallback_payload(reason: str, message: str = "") -> dict[str, Any]:
    """Build structured metadata for a batch text delivery path."""

    definition = _STREAM_FALLBACK_REASON_DEFINITIONS.get(reason)
    if definition is None:
        raise ValueError(f"Unknown stream fallback reason: {reason}")
    payload: dict[str, Any] = {
        "reason": reason,
        **{
            key: list(value) if isinstance(value, list) else value
            for key, value in definition.items()
        },
    }
    if message:
        payload["message"] = message
    return payload


def _stream_fallback_reasons(app: "FastAPI") -> dict[str, dict[str, Any]]:
    reasons = getattr(app.state, "stream_fallback_reasons", None)
    if not isinstance(reasons, dict):
        reasons = {}
        app.state.stream_fallback_reasons = reasons
    return reasons


def _record_stream_fallback(
    app: "FastAPI",
    sid: str,
    reason: str,
    message: str = "",
) -> None:
    _stream_fallback_reasons(app)[sid] = _stream_fallback_payload(reason, message)


def _pop_stream_fallback(app: "FastAPI", sid: str) -> dict[str, Any]:
    return _stream_fallback_reasons(app).pop(sid, {})


def _append_stream_listener(
    listeners: list[Any],
    stream_listener_cls: Any,
    *,
    signature_field_name: str,
    predict: Any,
) -> None:
    if predict is None:
        return
    try:
        listeners.append(
            stream_listener_cls(
                signature_field_name=signature_field_name,
                predict=predict,
            )
        )
    except Exception:  # noqa: BLE001
        return


def _build_stream_listeners(agent: Any, stream_listener_cls: Any) -> list[Any]:
    """Build explicit DSPy stream listeners for CLIO's known predictors.

    Auto-discovering by field name is fragile here because several CLIO
    predictors expose the same output fields. Explicit predictor binding
    lets chat, final synthesis, and expert outputs stream live without
    fighting over repeated names like ``answer`` or ``analysis``.
    """

    listeners: list[Any] = []
    _append_stream_listener(
        listeners,
        stream_listener_cls,
        signature_field_name="answer",
        predict=getattr(agent, "chat_agent", None),
    )
    _append_stream_listener(
        listeners,
        stream_listener_cls,
        signature_field_name="answer",
        predict=getattr(agent, "answer_synthesizer", None),
    )

    return listeners


def _agent_streaming_unsupported_reason(agent: Any) -> str:
    """Return a fallback reason when the active provider cannot stream live.

    Only the CLI-backed custom transports (``codex`` JSON-RPC, ``claude_code``
    exec) are genuinely non-streaming. Argonne/ALCF (Sophia + Metis) is a plain
    OpenAI-compatible SSE endpoint: it streams at the provider AND through LiteLLM
    (verified: multi-chunk incremental deltas), so it must NOT be force-classified
    as batch. Hardcoding it here bypassed the streamify pump for EVERY ALCF run
    (iowarp/clio-agent#160). The streamify path below has its own graceful
    try/except fallback to sync, so letting argonne attempt streaming can only
    improve on the previous always-batch behaviour.
    """

    provider_config = getattr(agent, "_provider_config", None)
    provider = str(getattr(provider_config, "provider", "") or "")
    provider_kind = _provider_runtime_kind(provider)
    if provider_kind in {"claude_code", "codex"}:
        return "provider_streaming_unsupported"
    # iowarp/clio-agent#639: normalize the preset id (argonne_sophia/_metis) to
    # the provider kind (argonne) BEFORE the capability check. Reasoning models on
    # the ALCF gateways stream their answer on the reasoning_content channel,
    # which DSPy's content-only stream listeners can't fold and which fails the
    # streamify task group ("live streaming failed before emitting output"). Route
    # them through the robust blocking path (which recovers reasoning_content via
    # _process_completion). Scoped to argonne reasoning models: non-reasoning ALCF
    # (gpt-oss/gemma) still streams (#160), and lm_studio reasoning models (qwopus)
    # stream content fine, so they are untouched.
    if provider_kind == "argonne" and _config_is_reasoning_model(provider_config):
        return "provider_streaming_unsupported"
    return ""


def _config_is_reasoning_model(provider_config: Any) -> bool:
    """Whether a provider config is a reasoning model (handshake ``is_reasoning``
    / per-model capability). Used to keep reasoning models off streaming paths
    that lose the reasoning_content channel."""

    if provider_config is None:
        return False
    try:
        from clio_agent.config import _reasoning_model_capability  # noqa: PLC0415

        return bool(_reasoning_model_capability(provider_config))
    except Exception:
        return bool(getattr(provider_config, "is_reasoning", False))


def _stream_response_prefix(field_name: str, previous_field_name: str) -> str:
    """Return formatting to insert when a streamed output field starts."""

    if not field_name or field_name == previous_field_name:
        return ""
    if field_name == "recommendations":
        return "\n\nRecommendations:\n"
    if field_name == "file_path":
        return "\n\nFile: "
    return ""


# Minimum gap between reasoning-channel heartbeats. The watchdog only needs
# *a* progress event within its window (default 900s), so a 1s throttle keeps a
# deep-reasoning turn alive without flooding the bus with one event per token.
_REASONING_HEARTBEAT_S = 1.0


def _describe_stream_exc(exc: BaseException) -> str:
    """Format a streaming exception for logging, UNWRAPPING ``ExceptionGroup``.

    ``streamify`` runs the agent forward inside an anyio task group, so a failure
    surfaces as ``ExceptionGroup`` whose ``str()`` is only the opaque wrapper
    ("unhandled errors in a TaskGroup (1 sub-exception)") — the real cause lives
    in ``.exceptions``. Recurse into the leaves so the captured detail names the
    actual provider/transport error instead of the wrapper.
    """
    group = getattr(exc, "exceptions", None)
    if group:
        leaves = "; ".join(_describe_stream_exc(sub) for sub in group)
        return f"{type(exc).__name__}[{leaves}]"
    return f"{type(exc).__name__}: {exc}"


async def _try_streamed_forward(
    app: "FastAPI",
    enriched_text: str,
    sid: str,
    emit_chunk,
    session_mode: str = "chat",
    session_edit_mode: str = "diff",
    agent_override: Any | None = None,
    cancel_requested: Any | None = None,
) -> Optional[Any]:
    """Run the agent's forward via dspy.streamify, pumping every
    text chunk through ``emit_chunk(text)`` as it arrives. Returns
    the final dspy.Prediction on success, or None if streaming is
    unavailable before invoking the agent. Streaming execution failures
    raise ``_StreamingOutputError`` so the caller can surface the failed
    turn instead of rerunning it as batch fallback text.

    Falls back before output when the agent isn't a DSPy module, when
    streamify import fails, or when the wrapped call doesn't yield
    parsable text chunks. The fallback synchronous path produces
    the same wire shape (just no live deltas).
    """

    # Guided/structured output streams as reasoning_content-only deltas on
    # LM Studio (no content deltas), which the assembly below can't fold into
    # content -> empty content -> parse failure. Return None so the caller falls
    # back to the blocking path, whose content<-reasoning_content fallback
    # (_process_completion) recovers the constrained JSON. TODO: fold reasoning
    # deltas into the stream assembly to re-enable live streaming under guided output.
    try:
        from clio_agent.config import _guided_output_enabled  # noqa: PLC0415

        if _guided_output_enabled():
            _record_stream_fallback(app, sid, "stream_disabled_guided_output")
            return None
    except Exception:  # noqa: BLE001 - never let this gate break the turn
        pass

    # Some reasoning-model + provider combos stream the answer entirely on the
    # reasoning_content delta channel (which content-only stream listeners miss
    # and which bypasses _process_completion's content<-reasoning_content
    # recovery) or fail the streamify task group outright. Routing them through
    # the blocking path recovers the answer. Default ON (unchanged for every
    # model that streams cleanly); opt out per model via CLIO_LIVE_STREAMING=0.
    try:
        from clio_agent.config import _live_streaming_enabled  # noqa: PLC0415

        if not _live_streaming_enabled():
            _record_stream_fallback(app, sid, "stream_disabled_live_streaming")
            return None
    except Exception:  # noqa: BLE001 - never let this gate break the turn
        pass

    try:
        import dspy  # noqa: PLC0415
        from dspy.streaming.messages import StreamResponse  # noqa: PLC0415
        from dspy.streaming.streamify import streamify
        from dspy.streaming.streaming_listener import StreamListener  # noqa: PLC0415
        from litellm.types.utils import ModelResponseStream  # noqa: F401
    except Exception as exc:
        _record_stream_fallback(
            app,
            sid,
            "streaming_dependency_unavailable",
            f"{type(exc).__name__}: {exc}",
        )
        return None

    agent = agent_override if agent_override is not None else app.state.agent
    if agent is None:
        _record_stream_fallback(app, sid, "agent_not_available")
        return None
    if not isinstance(agent, dspy.Module):
        _record_stream_fallback(app, sid, "agent_not_streamable")
        return None
    unsupported_reason = _agent_streaming_unsupported_reason(agent)
    if unsupported_reason:
        _record_stream_fallback(app, sid, unsupported_reason)
        return None

    # iowarp/clio-agent#158: bind listeners to explicit Predict instances
    # instead of asking DSPy to infer them by output field name.
    listeners = _build_stream_listeners(agent, StreamListener)
    # is_async_program=True is only valid for modules with a real async
    # forward implementation. dspy.Module exposes acall generically, but
    # its default implementation delegates to aforward; ClioAgent only has
    # sync forward today, so treating inherited acall as sufficient forces
    # streamify into AttributeError and silently drops to synthetic fallback.
    has_async_forward = callable(getattr(agent, "aforward", None))
    try:
        streamed = streamify(
            agent,
            async_streaming=True,
            stream_listeners=listeners,
            is_async_program=has_async_forward,
        )
    except Exception as exc:
        # Stream binding is best-effort. If DSPy cannot attach the
        # listener to this program shape, let the canonical sync path
        # run and surface any real agent/provider error from there.
        _record_stream_fallback(
            app,
            sid,
            "stream_setup_failed",
            f"{type(exc).__name__}: {exc}",
        )
        return None

    final_pred = None
    emitted_any = False
    previous_stream_field = ""
    # Seed the reasoning-heartbeat clock so the first reasoning chunk publishes
    # immediately (refreshing the watchdog the moment the model starts thinking).
    last_reasoning_heartbeat = time.monotonic() - _REASONING_HEARTBEAT_S

    async def _emit_visible_chunk(text: str, field_name: str = "") -> None:
        nonlocal emitted_any, previous_stream_field
        prefix = _stream_response_prefix(field_name, previous_stream_field)
        if prefix:
            await emit_chunk(prefix)
            emitted_any = True
        await emit_chunk(text)
        emitted_any = True
        if field_name:
            previous_stream_field = field_name

    try:
        # StreamListener emits ``StreamResponse`` instances that
        # carry the cleaned chunk in ``.chunk``. Keep the legacy
        # ``ModelResponseStream`` / dict / str fallback for backends
        # that don't surface a typed listener payload.
        # Pass session_mode + session_edit_mode if the agent's
        # forward signature accepts them (newer ClioAgent does;
        # older / fake agents fall back via TypeError catch).
        try:
            stream_iter = streamed(
                question=enriched_text,
                session_id=sid,
                session_mode=session_mode,
                session_edit_mode=session_edit_mode,
                cancel_requested=cancel_requested,
            )
        except TypeError:
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
                    await _emit_visible_chunk(
                        piece.chunk, getattr(piece, "signature_field_name", "") or ""
                    )
                continue
            text_chunk = _chunk_text(piece)
            if text_chunk:
                await _emit_visible_chunk(text_chunk)
                continue
            # No answer-content in this chunk -- but the model may be actively
            # streaming REASONING tokens (a separate delta channel invisible to
            # DSPy's content-only listeners). Publishing a throttled, session-
            # scoped heartbeat refreshes the no-progress watchdog so a deep-
            # reasoning expert call isn't killed mid-think. We DON'T route the
            # reasoning into the answer part (it would pollute the answer); the
            # event carries it under a distinct type a TUI may render as
            # "thinking", and -- crucially -- advances bus.last_publish_monotonic.
            reasoning_chunk = _chunk_reasoning_text(piece)
            if reasoning_chunk:
                now = time.monotonic()
                if now - last_reasoning_heartbeat >= _REASONING_HEARTBEAT_S:
                    last_reasoning_heartbeat = now
                    try:
                        app.state.bus.publish(
                            Event(
                                type="agent.reasoning.delta",
                                session_id=sid,
                                payload={"stream_source": "reasoning"},
                            )
                        )
                    except Exception:  # noqa: BLE001 - heartbeat is best-effort
                        pass
    except Exception as exc:
        detail = _describe_stream_exc(exc)
        if emitted_any:
            raise _StreamingOutputError(
                f"live streaming failed after emitting output: {detail}"
            ) from exc
        _record_stream_fallback(
            app,
            sid,
            "stream_failed_before_output",
            detail,
        )
        raise _StreamingOutputError(
            f"live streaming failed before emitting output: {detail}"
        ) from exc
    if emitted_any and final_pred is None:
        raise _StreamingOutputError(
            "live streaming ended after emitting output without a final prediction"
        )
    if final_pred is None:
        _record_stream_fallback(app, sid, "stream_no_prediction")
    elif not emitted_any:
        _record_stream_fallback(
            app,
            sid,
            "stream_completed_without_chunks",
            "DSPy streamify returned a final prediction but emitted no visible text chunks.",
        )
    return final_pred


def _chunk_reasoning_text(piece: Any) -> str:
    """Pull reasoning-channel text out of a streamify chunk.

    Reasoning models (qwopus, nemotron, …) stream their chain-of-thought on a
    SEPARATE delta channel (``delta.reasoning_content`` / ``delta.reasoning``),
    not ``delta.content``. DSPy's StreamListener only watches ``delta.content``
    for ``[[ ## field ## ]]`` markers, so reasoning tokens are invisible to it.
    For an unlistened predict (every blueprint expert), streamify yields the raw
    chunk straight through to our pump -- but ``_chunk_text`` returns "" for it
    (content is empty during thinking). We extract the reasoning channel here so
    the pump can refresh the no-progress watchdog while the model is *actively
    thinking* (a deep-reasoning expert call can stream tens of thousands of
    reasoning tokens with zero answer-content tokens; treating that as "no
    progress" wrongly kills a working model -- see the EarthScope resolver hang).
    """

    if not piece or isinstance(piece, (str, dict)):
        # dict shape handled below in the rare OpenAI-dict path; str is answer text.
        if isinstance(piece, dict):
            try:
                delta = piece["choices"][0]["delta"]
                return str(delta.get("reasoning_content") or delta.get("reasoning") or "")
            except (KeyError, IndexError, TypeError):
                return ""
        return ""
    try:
        choices = piece.choices  # type: ignore[attr-defined]
        if choices:
            delta = getattr(choices[0], "delta", None)
            if delta is not None:
                reasoning = getattr(delta, "reasoning_content", None) or getattr(
                    delta, "reasoning", None
                )
                if reasoning:
                    return str(reasoning)
    except Exception:  # noqa: BLE001 - best-effort extraction
        pass
    return ""


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
    # Mode gate — plan + architect can't apply.
    if session.mode in {"plan", "architect"}:
        raise PermissionError(f"refused to write under session.mode={session.mode!r}")
    target = validate_write_path(path, field="path")

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

    return write_text_with_policy(str(target), new_content)


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


# Core no longer bundles in-process scientific format servers, so it ships no
# built-in binary context inspectors. Structured inspection of attached binary
# files (parquet/hdf5/...) is the job of declared MCP tools the active pack
# brings in. The extension -> inspector map stays as a generic, currently-empty
# hook so the context-file path below is unchanged.
_BINARY_CONTEXT_INSPECTORS: dict[str, Any] = {}


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
            thought = traj.get(f"step_{idx}_thought") or traj.get(f"thought_{idx}")
            action = traj.get(f"step_{idx}_tool_name") or traj.get(f"action_{idx}")
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
        agent_trace_call = False
        if isinstance(call, dict):

            def get(key: str, default: Any = None, _src: Any = call) -> Any:
                return _src.get(key, default)
        else:
            # msgspec structs + DSPy trace records — attribute access.
            def get(key: str, default: Any = None, _src: Any = call) -> Any:
                return getattr(_src, key, default)

            agent_trace_call = (
                hasattr(call, "tool") and hasattr(call, "params") and hasattr(call, "result")
            )

        name = get("name") or get("tool") or ""
        if name:
            row["name"] = str(name)

        args = get("args")
        if args is None:
            args = get("arguments")
        if args is None:
            args = get("params")
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

        result = get("result")
        if result is not None:
            row["result"] = _bounded_tool_call_result(result)
            if "ok" not in row and agent_trace_call:
                row["ok"] = not (
                    (isinstance(result, dict) and "error" in result)
                    or (isinstance(result, str) and result.startswith("Error:"))
                )

        telemetry_source = get("telemetry_source") or (
            "agent_trace" if agent_trace_call else "posthoc_prediction"
        )
        row["telemetry_source"] = str(telemetry_source)

        if row:
            out.append(row)
    return out


def _signature_prompt(signature: Any) -> str:
    """Return a cleaned DSPy signature docstring for catalog display."""
    return inspect.cleandoc(getattr(signature, "__doc__", "") or "")


# --- re-export shim (#714): skills/commands/catalog loading moved to catalog.py ---
from typing import Protocol

from clio_agent.gact.agent_blueprints import (
    discover_agent_blueprints,
    load_agent_blueprint_path,
    load_agent_blueprints,
    load_mcp_descriptors,
    read_install_metadata,
    validate_agent_hierarchy,
)
from clio_agent.gact.catalog import (  # noqa: E402, F401
    _builtin_agents,
    _builtin_tools,
    _command_search_roots,
    _default_skill_id,
    _fallback_skill_keywords,
    _load_command_files_from_disk,
    _load_skills_from_disk,
    _normalize_file_command_id,
    _parse_skill_frontmatter,
    _skill_list_field,
    _skill_markdown_files,
    _skill_search_roots,
    _tool_owner_for_catalog,
    _tool_tags_for_catalog,
    _tool_visible_to_for_catalog,
    _truthy_command_field,
)
from clio_agent.gact.events import Event, EventBus
from clio_agent.gact.expert_packs import (
    discover_expert_packs,
    load_expert_pack_path,
    load_expert_packs,
    validate_expert_hierarchy,
)
from clio_agent.gact.messages import MessageStore
from clio_agent.gact.sessions import SessionStore, _default_store_path
from clio_agent.gact.types import (
    AgentCapabilityRef,
    AgentDef,
    AnswerUserQuestionRequest,
    CreateSessionRequest,
    CreateUserQuestionRequest,
    ErrorEnvelope,
    ErrorInfo,
    ListSessionsResponse,
    LMProviderInfo,
    LMProviderPreset,
    LMProviderRequest,
    Message,
    ModelRef,
    Part,
    PostMessageRequest,
    PostMessageResponse,
    RetryTurnRequest,
    Session,
    Tokens,
    TurnAttempt,
    UpdateSessionRequest,
    UserQuestion,
    UserQuestionOption,
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




@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hook.

    Spins the scheduler tick task (#21) at boot if a ScheduleStore
    is wired; cancels it cleanly on shutdown.

    Also kicks off deferred ClioAgent construction when the runner
    set ``app.state.want_agent`` (see ``main()``). The agent's heavy
    init (DSPy + ARC + experts) used to block uvicorn's startup, which
    pushed first /v1/capabilities response past gact-tui's 3-second
    deploy probe. Now we bind the port immediately, finish boot in a
    background task, and POST /messages keeps 503-ing until
    ``app.state.agent`` is stamped.
    """

    app.state.started_at = time.time()
    task: Optional[asyncio.Task] = None
    if getattr(app.state, "schedules", None) is not None:
        task = asyncio.create_task(_scheduler_tick(app))
        app.state.scheduler_task = task

    agent_task: Optional[asyncio.Task] = None
    if getattr(app.state, "want_agent", False) and app.state.agent is None:
        agent_task = asyncio.create_task(_construct_agent_async(app))
        app.state.agent_construction_task = agent_task

    yield

    lm_config_task = getattr(app.state, "lm_config_task", None)
    for t in (task, agent_task, lm_config_task):
        if t is None:
            continue
        if getattr(t, "done", lambda: False)():
            continue
        t.cancel()
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: _release_owned_lm_studio_instance(app, raise_on_error=False),
        )
    except Exception:
        pass
    # Drain + stop the off-loop semantic-trace writer so no events are lost on shutdown.
    _trace_backend = getattr(app.state, "semantic_trace_backend", None)
    _trace_close = getattr(_trace_backend, "close", None)
    if callable(_trace_close):
        try:
            _trace_close()
        except Exception:  # pragma: no cover - defensive shutdown cleanup
            pass
    if getattr(app.state, "tool_hooks_installed", False):
        try:
            from clio_agent.tools.execution import (  # noqa: PLC0415
                set_global_cancellation_checker,
                set_global_permission_gate,
                set_global_tool_interceptor,
                set_global_tool_observer,
            )

            set_global_cancellation_checker(None)
            set_global_permission_gate(None)
            set_global_tool_interceptor(None)
            set_global_tool_observer(None)
        except Exception:  # pragma: no cover - defensive shutdown cleanup
            pass


async def _construct_agent_async(app: "FastAPI") -> None:
    """Build the real ClioAgent off the lifespan hot path.

    DSPy import + ARC hydration + expert wiring takes ~10 s on Aurora's
    frameworks Python (beartype import hook + Lustre cold reads). We
    run it via ``run_in_executor`` so the event loop stays free for
    /v1/capabilities, /v1/health, and the rest of the catalog while
    the agent constructs. On success, stamps ``app.state.agent`` +
    ``app.state.arc`` so the next POST /messages dispatches normally;
    on failure, logs and leaves ``agent=None`` so /messages keeps
    surfacing a structured 503 instead of a corrupted half-built
    agent.
    """

    loop = asyncio.get_running_loop()
    # Construct (or reuse) the ONE per-process ARC up front and inject it into the build,
    # so the agent does not mint a fresh ARC — the same instance is app.state.arc for the
    # whole process across every later LM bind (no per-build ARC churn / trace ⊋ ARC split).
    arc = _process_arc(app)

    def _build() -> Any:
        import dspy  # noqa: PLC0415

        from clio_agent.agent import ClioAgent  # noqa: PLC0415
        from clio_agent.config import (  # noqa: PLC0415
            create_chat_adapter,
            create_lm,
            load_config_from_env,
        )

        cfg = load_config_from_env()
        dspy.configure(
            lm=create_lm(cfg),
            adapter=create_chat_adapter(cfg),
        )
        return ClioAgent(verbose=False, arc=arc)

    try:
        agent = await loop.run_in_executor(None, _build)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[clio-agent-gact] deferred agent init failed ({exc!r}); "
            "POST /messages will keep returning 503.",
            flush=True,
        )
        app.state.agent_init_error = repr(exc)
        return

    app.state.agent = agent
    # The agent's ARCMemory is built HERE (async), after build_app ran with arc=None;
    # _set_app_arc (re)wires the arc.op op-logger so ARC writes are observable.
    _set_app_arc(app, agent.arc)

    # Install the deferred permission gate + tool observer now that we
    # know an agent exists to gate. See build_app for why these aren't
    # installed at construction time.
    try:
        _install_tool_runtime_hooks(app)
    except Exception:  # pragma: no cover - defensive
        pass

    print("[clio-agent-gact] agent ready.", flush=True)


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
                scheduled_user_msg_id = _new_message_id("user")
                user_msg = Message(
                    id=scheduled_user_msg_id,
                    # A scheduled turn correlates to its own user message id (#711).
                    turn_id=scheduled_user_msg_id,
                    session_id=sch.session_id,
                    role="user",
                    created_at=_iso_from_epoch(time.time()),
                    updated_at=_iso_from_epoch(time.time()),
                    parts=[
                        Part(
                            id=_new_part_id(),
                            type="text",
                            text=sch.question,
                        )
                    ],
                    metadata={"scheduled": True, "schedule_id": sch.id},
                )
                _append_session_message(app, sch.session_id, user_msg)
                app.state.bus.publish(
                    Event(
                        type="message.created",
                        session_id=sch.session_id,
                        payload=user_msg.model_dump(exclude_none=True),
                    )
                )
                app.state.schedules.mark_fired(sch.id)
                # Fire-and-forget the turn task.
                asyncio.create_task(
                    _run_turn_in_background(
                        app,
                        sch.session_id,
                        sch.question,
                        user_msg,
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

    # CORS: browser/WebView frontends must opt in with explicit origins.
    # CLIO's default auth scheme is trust_socket, which is safe for local
    # non-browser clients but must not grant arbitrary browser origins access
    # to a localhost agent. Operators can enable trusted web origins with
    # CLIO_GACT_CORS_ORIGINS (comma-separated origins or "*").
    cors_origins_env = os.environ.get("CLIO_GACT_CORS_ORIGINS", "").strip()
    if cors_origins_env:
        allow_origins: list[str] = (
            ["*"]
            if cors_origins_env == "*"
            else [o.strip() for o in cors_origins_env.split(",") if o.strip()]
        )
    else:
        allow_origins = []
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
    # Initialise state eagerly in case the caller skips the lifespan
    # context (TestClient normally runs it, but older FastAPI + some
    # test-utility paths don't).
    app.state.started_at = time.time()
    session_store_path = sessions_path if sessions_path is not None else _default_store_path()
    app.state.sessions = SessionStore(path=session_store_path)
    app.state.agent = agent  # may be None; POST message checks before using
    _set_app_arc(app, arc)  # arc may be None; /v1/memory/stats returns zeros then
    prompt_write_root = session_store_path.parent / "prompts"
    app.state.prompt_registry = PromptRegistry(
        sources=[
            PromptSource("global", prompt_write_root),
            PromptSource("workspace", Path.cwd() / ".clio" / "prompts"),
        ],
        write_root=prompt_write_root,
    )
    app.state.memory_events = {}
    app.state.command_audit = []
    # CLIO-BBBBBBBBBB13: per-session pub/sub. POST /messages
    # publishes; /v1/sessions/{sid}/events subscribers consume.
    app.state.bus = EventBus()
    app.state.semantic_trace_detail_level = (
        os.environ.get(
            "CLIO_SEMANTIC_TRACE_DETAIL",
            DEFAULT_DETAIL_LEVEL,
        ).strip()
        or DEFAULT_DETAIL_LEVEL
    )
    app.state.semantic_trace_backend = build_trace_backend(
        session_store_path.parent / "semantic_traces"
    )
    # ARC-as-source: the sink has NO arc live_consumer. ARC is the SOURCE now —
    # _emit_semantic_event routes each event through arc.record_semantic_event, which
    # folds the observer (on_semantic_event) INSIDE its record and then derives THIS
    # sink. Registering arc.on_semantic_event here too would double-fold; routing the
    # sink back into arc would recurse. So live_consumers stays empty: arc.record ->
    # sink.emit -> {trace, SSE, hooks} (no arc), a strict one-way derivation.
    app.state.semantic_event_sink = SemanticEventSink(
        bus=app.state.bus,
        trace_backend=app.state.semantic_trace_backend,
        detail_level=app.state.semantic_trace_detail_level,
        live_consumers=None,
    )
    # (ARC's arc.op op-logger AND highway-derive sink are wired via _set_app_arc
    # whenever app.state.arc is assigned — see _set_app_arc; the highway closure reads
    # app.state.semantic_event_sink at fire-time, so this construction order is fine.)
    # CLIO-BBBBBBBBBB14: message log keyed by session_id. Populated by
    # POST /messages, read by GET /messages, and backed by per-session
    # JSON ledgers so adapter deletion/redeploy preserves transcripts.
    app.state.message_store = MessageStore(path=session_store_path.parent / "messages")
    app.state.messages = app.state.message_store.load_all()
    # CLIO-BBBBBBBBBB20: cooperative cancellation flags. POST /cancel
    # adds a sid; the POST-message handler checks + clears after the
    # agent returns. Set (not dict) because the flag's presence IS
    # the signal — no payload.
    app.state.cancel_flags = set()
    app.state.cancel_events = {}
    app.state.cancel_attempts = {}
    # CLIO-BBBBBBBBBB22: per-session context files. Keyed by
    # session_id, each value is an ordered dict of
    # path -> ContextFile dict.
    app.state.context_files_path = session_store_path.parent / "context_files.json"
    app.state.context_files = _load_context_files(app.state.context_files_path)
    # iowarp/clio-agent#331: per-turn context truth frames. These
    # capture what visible transcript/context attachments were
    # retained for a turn, plus model/agent/prompt provenance.
    app.state.context_frames = {}
    # iowarp/clio-agent#369: agent-callable memory tool audit. Tool
    # reads are policy-gated and provenance-bearing so cross-session
    # context is visible after the fact.
    app.state.memory_tool_audit = []
    # CLIO-BBBBBBBBBB21: per-session pending diffs. Keyed by
    # session_id -> list of {path, unified_diff, status,
    # part_id, message_id}. Status is "pending" until apply/reject
    # flips it.
    app.state.pending_diffs = {}
    # CLIO-BBBBBBBBBB23: pending permission requests. Flat dict
    # keyed by permission_id so GET /v1/permissions can filter by
    # session cheaply. Each record carries
    # {id, session_id, tool_call, summary, created_at, status,
    #  action, resolved_at}.
    app.state.permissions = {}
    # iowarp/clio-agent#7: per-permission threading.Event so the
    # MCPToolBridge gate (running in a worker thread) can block on
    # the user's response without polling.
    app.state.permission_events = {}
    # iowarp/clio-agent#333: structured ask-user protocol. The
    # orchestrator/backend can publish pending questions; clients
    # answer or cancel them through explicit endpoints.
    app.state.user_questions = {}
    # iowarp/clio-agent#333: retry attempts preserve provenance for
    # retry-with-notes/model flows without mutating the original turn.
    app.state.turn_attempts = {}
    # SPEC §6.17 hooks (declarative event→command/url callouts that
    # gact-tui drives via /v1/hooks). Distinct from CLIO's runtime
    # in-process Python hooks (clio_agent.runtime.hooks) — these are
    # user-configurable callouts the agent fires during the turn
    # lifecycle, while the Python runtime hooks are framework-level
    # extension points. In-memory; not persisted across restarts.
    app.state.declarative_hooks = {}
    # SPEC §6.11.b permission policies — list, not dict. Backends
    # consult this on every tool call to decide allow/deny/ask before
    # falling back to the per-tool permission_default. PUT replaces
    # the whole list.
    app.state.permission_policies_path = session_store_path.parent / "permission_policies.json"
    app.state.permission_policies = _load_permission_policies(app.state.permission_policies_path)
    # iowarp/clio-agent#18: per-session task list (todo-style).
    # Keyed by session_id -> {task_id -> task dict}. In-memory.
    app.state.session_tasks = {}
    # iowarp/clio-agent#3: per-session in-flight turn tasks. POST
    # /messages tracks the asyncio.Task here so /cancel can
    # hard-abort instead of waiting for the cooperative flag check.
    app.state.in_flight_turns = {}
    # iowarp/clio-agent#2: per-session ledger of tool calls observed
    # during the in-flight turn. The global tool_observer appends
    # here; _run_turn_in_background drains it post-forward to attach
    # tools_called metadata even when the underlying expert
    # didn't populate ``pred.tools_called`` itself.
    app.state.tool_call_ledger = {}
    # In-flight assistant message/parts emitted from real runtime
    # boundaries before the final assistant message is persisted. This
    # lets SSE clients show tool calls and delegations as they happen.
    app.state.live_assistant_message_ids = {}
    app.state.live_assistant_parts = {}
    app.state.live_assistant_part_keys = {}

    # iowarp/clio-agent#7 + #2: install process-global hooks on the
    # MCPToolBridge so EVERY expert's tool call routes through our
    # permission gate + telemetry observer.
    #
    # When an agent is already in hand we install eagerly — that's
    # the legacy build_app(agent=X) path tests use. When the caller
    # left agent=None (the production main() flow that defers
    # ClioAgent construction to the lifespan task) we stash the
    # closures on app.state and install them right after the agent
    # finishes constructing — importing clio_agent.tools.execution
    # transitively pulls litellm + dspy (~4 s) and we need build_app
    # to stay cheap enough for gact-tui's 3-second deploy probe.
    #
    # Expose the gate/observer CONSTRUCTORS on app.state so runtime code carved
    # out of this module (#714 decomposition) can build a fresh gate/observer
    # WITHOUT importing _make_permission_gate/_make_tool_observer from app.py
    # (which would reintroduce the no-cycle violation). Callers prefer the
    # already-installed app.state.pending_permission_gate/pending_tool_observer
    # and fall back to these factories — mirroring _call_enabled_external_mcp_tool.
    app.state.make_permission_gate = lambda: _make_permission_gate(app)
    app.state.make_tool_observer = lambda: _make_tool_observer(app)
    if agent is not None:
        try:
            _install_tool_runtime_hooks(app)
        except Exception:  # pragma: no cover - defensive
            pass
    else:
        app.state.tool_hooks_installed = False
        app.state.pending_cancellation_checker = _make_cancellation_checker(app)
        app.state.pending_permission_gate = _make_permission_gate(app)
        app.state.pending_tool_observer = _make_tool_observer(app)

    # iowarp/clio-agent#20: install the user-hooks registry so
    # pre_tool / post_tool / pre_message / post_message events
    # route to ~/.config/clio-agent/hooks/<event>.py. Tests pre-
    # install their own registry; we only install a default if
    # nothing's currently wired so the test-side hook stays.
    try:
        from clio_agent.runtime.hooks import (
            _registry as _current_registry,
        )
        from clio_agent.runtime.hooks import (
            build_hook_registry,
            install_global_registry,
        )

        if _current_registry is None:
            registry = build_hook_registry()
            install_global_registry(registry)
            app.state.runtime_hook_registry_metadata = (
                registry.metadata() if hasattr(registry, "metadata") else {}
            )
        else:
            app.state.runtime_hook_registry_metadata = (
                _current_registry.metadata() if hasattr(_current_registry, "metadata") else {}
            )
    except Exception:  # pragma: no cover - defensive
        app.state.runtime_hook_registry_metadata = {
            "backend": "unavailable",
            "enabled": False,
            "error": "failed_to_initialize",
        }
        pass

    # CLIO-BBBBBBBBBB-D: live LM config — what the TUI configured
    # us with. Distinct from boot-time env because PUT /providers/lm
    # rebuilds the agent + DSPy config in-place.
    app.state.lm_config = None
    app.state.lm_config_status = {"state": "idle"}
    app.state.lm_config_task = None
    app.state.lm_studio_owned_instance = None
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
        path=(sessions_path.parent / "agents.json") if sessions_path is not None else _ua_default()
    )
    # iowarp/clio-agent#21: scheduled turns store + tick task.
    from clio_agent.gact.scheduler import ScheduleStore as _SchedStore

    app.state.schedules = _SchedStore(
        path=(sessions_path.parent / "schedules.json") if sessions_path is not None else None
    )
    app.state.scheduler_task = None
    # iowarp/clio-agent#22: shared session tokens.
    app.state.shared_tokens = {}

    # ---- /v1/health + /v1/capabilities + /v1/capability-gaps + /v1/metrics ----
    # + /v1/memory/stats: the read-only system/observability surface is owned by
    # routes/system.py and registered below via register_system_routes(app, deps)
    # once ``deps`` is built. The static capability/metrics catalogs they project
    # live in runtime/capabilities.py (shared with the message-turn streaming
    # path here); the wire/limit constants live in runtime/constants.py.

    # ---- 501 stubs for the rest of the surface ---------------------------
    # Every route in the v0.2 contract that we haven't wired yet
    # returns the structured error envelope from above. Matches the
    # shape v0.2 clients expect, while honestly reporting that the
    # backend doesn't yet implement the endpoint.

    # ---- /v1/prompts (CLIO prompt registry) ------------------------------

    def _prompt_workspace_root(workspace_id: str = "", session_id: str = "") -> Path:
        wid = workspace_id
        if session_id:
            sess = app.state.sessions.get(session_id)
            if sess is not None:
                wid = wid or str(getattr(sess, "workspace_id", "") or "")
        if wid:
            ws = app.state.workspaces.get(wid)
            if ws is not None:
                root_path = str(getattr(ws, "root_path", "") or "")
                if root_path:
                    return Path(root_path).expanduser()
        return Path.cwd()

    def _active_prompt_pack_path(session_id: str = "") -> Path | None:
        if not session_id:
            return None
        sess = app.state.sessions.get(session_id)
        if sess is None:
            return None
        metadata = getattr(sess, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            return None
        raw = str(metadata.get("active_expert_pack_path") or "").strip()
        return Path(raw).expanduser() if raw else None

    def _prompt_sources_for_request(
        *,
        session_id: str = "",
        workspace_id: str = "",
    ) -> list[PromptSource]:
        cwd = _prompt_workspace_root(workspace_id=workspace_id, session_id=session_id)
        sources = [
            PromptSource("global", prompt_write_root),
        ]
        for pack in discover_expert_packs(cwd=cwd):
            prompt_root = pack.root / "prompts"
            if prompt_root.is_dir():
                sources.append(PromptSource(f"{pack.scope}_pack", prompt_root))
        sources.append(PromptSource("workspace", cwd / ".clio" / "prompts"))
        if session_id:
            active_blueprint_path = _active_session_agent_blueprint_path(session_id)
            active_blueprint_id = _active_session_agent_blueprint_id(session_id)
            active_blueprint_root = active_blueprint_path
            if active_blueprint_root is None and active_blueprint_id:
                active = next(
                    (
                        row
                        for row in discover_agent_blueprints(cwd=cwd)
                        if row.id == active_blueprint_id
                    ),
                    None,
                )
                active_blueprint_root = active.root if active is not None else None
            if active_blueprint_root is not None and (active_blueprint_root / "prompts").is_dir():
                sources.append(
                    PromptSource("session_agent_blueprint", active_blueprint_root / "prompts")
                )
        active_pack_path = _active_prompt_pack_path(session_id)
        if active_pack_path is not None and (active_pack_path / "prompts").is_dir():
            sources.append(PromptSource("session_pack", active_pack_path / "prompts"))
        if session_id:
            sources.append(
                PromptSource("session", prompt_write_root.parent / "session-prompts" / session_id)
            )
        return sources

    def _prompt_write_root_for_request(
        *,
        scope: str,
        session_id: str = "",
        workspace_id: str = "",
    ) -> Path:
        if scope == "session":
            if not session_id:
                raise ValueError("session_id is required for session prompt writes")
            return prompt_write_root.parent / "session-prompts" / session_id
        if scope == "workspace":
            cwd = _prompt_workspace_root(workspace_id=workspace_id, session_id=session_id)
            return cwd / ".clio" / "prompts"
        if scope in {"global", "user", ""}:
            return prompt_write_root
        raise ValueError("scope must be global, workspace, or session")

    def _prompt_registry_for_request(
        *,
        session_id: str = "",
        workspace_id: str = "",
        write_scope: str = "global",
    ) -> PromptRegistry:
        sources = _prompt_sources_for_request(session_id=session_id, workspace_id=workspace_id)
        return PromptRegistry(
            sources=sources,
            builtins=app.state.prompt_registry._builtins(),
            write_root=_prompt_write_root_for_request(
                scope=write_scope,
                session_id=session_id,
                workspace_id=workspace_id,
            ),
        )

    app.state.prompt_registry_for_request = _prompt_registry_for_request

    def _prompt_agent_overlay_for_request(session_id: str = "") -> dict[str, Any]:
        if not session_id:
            return {}
        overlay = _session_agent_overlay(session_id)
        agents = overlay.get("agents") if isinstance(overlay, Mapping) else None
        if not isinstance(agents, Mapping):
            return {}
        rows: list[dict[str, Any]] = []
        prompt_fields = {
            "system_prompt",
            "prompt_id",
            "prompt_profile",
            "default_provider",
            "default_model",
        }
        for agent_id, raw_patch in sorted(agents.items(), key=lambda item: str(item[0])):
            if not isinstance(raw_patch, Mapping):
                continue
            fields = sorted(str(key) for key in raw_patch if str(key) in prompt_fields)
            if not fields:
                continue
            rows.append(
                {
                    "agent_id": str(agent_id),
                    "fields": fields,
                    "has_system_prompt": bool(str(raw_patch.get("system_prompt") or "").strip()),
                    "prompt_id": str(raw_patch.get("prompt_id") or "").strip(),
                    "prompt_profile": str(raw_patch.get("prompt_profile") or "").strip(),
                    "default_provider": str(raw_patch.get("default_provider") or "").strip(),
                    "default_model": str(raw_patch.get("default_model") or "").strip(),
                    "source": "session_agent_overlay",
                    "session_id": session_id,
                }
            )
        return {
            "session_id": session_id,
            "source": "session_agent_overlay",
            "agents": rows,
        }

    def _prompt_render_context_for_request(
        *,
        session_id: str = "",
        workspace_id: str = "",
    ) -> dict[str, str]:
        context = _prompt_render_context(app)
        if session_id or workspace_id:
            try:
                agents = [
                    row
                    for row in _agent_rows(session_id=session_id, workspace_id=workspace_id)
                    if row.enabled
                ]
                by_parent: dict[str, list[AgentDef]] = {}
                for agent in agents:
                    by_parent.setdefault(agent.parent_id or "", []).append(agent)

                def render_tree(parent_id: str = "", depth: int = 0) -> list[str]:
                    lines: list[str] = []
                    for agent in sorted(
                        by_parent.get(parent_id, []), key=lambda row: (row.tier, row.id)
                    ):
                        indent = "  " * depth
                        detail = f" - {agent.description}" if agent.description else ""
                        lines.append(f"{indent}- {agent.id}: {agent.title}{detail}")
                        lines.extend(render_tree(agent.id, depth + 1))
                    return lines

                context["agents.available_tree"] = (
                    "\n".join(render_tree()) or "(no enabled experts)"
                )
                context["agents.available_flat"] = (
                    "\n".join(
                        f"- {agent.id}: {agent.title}"
                        for agent in sorted(agents, key=lambda row: row.id)
                    )
                    or "(no enabled experts)"
                )
            except Exception:
                pass
            if session_id:
                pack_id = ""
                blueprint_id = ""
                agent_id = ""
                sess = app.state.sessions.get(session_id)
                if sess is not None:
                    agent_id = _session_agent_id(sess)
                    metadata = getattr(sess, "metadata", {}) or {}
                    if isinstance(metadata, Mapping):
                        pack_id = str(metadata.get("active_expert_pack_id") or "").strip()
                        blueprint_id = str(metadata.get("active_agent_blueprint_id") or "").strip()
                context["session.active_pack"] = pack_id or "(no active expert pack)"
                context["session.active_agent_blueprint"] = (
                    blueprint_id or "(no active agent blueprint)"
                )
                try:
                    commands = [
                        f"- {row.get('id')}: {row.get('description') or row.get('title')}"
                        for row in _planner_command_rows(
                            app,
                            _resolve_runtime_dynamic_agent,
                            agent_id=agent_id,
                            cwd=_command_cwd_for_request(app, session_id),
                        )
                    ]
                    context["commands.agent_invocable"] = (
                        "\n".join(commands) or "(no agent-invocable commands)"
                    )
                except Exception:
                    pass
        return context

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
            model=req.model.model_dump(exclude_none=True) if req.model else None,
            agent=req.agent.model_dump(exclude_none=True) if req.agent else None,
            mode=req.mode,
            edit_mode=req.edit_mode,
            routing_mode=req.routing_mode,
        )
        _mirror_workspace_session(app, sess.id)
        return Session(**sess.to_wire())

    @app.patch("/v1/sessions/{sid}", response_model=Session)
    async def patch_session(sid: str, req: UpdateSessionRequest) -> Session:
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
            model=req.model.model_dump(exclude_none=True) if req.model else None,
            agent=req.agent.model_dump(exclude_none=True) if req.agent else None,
            # iowarp/gact-tui §audit/E-14: persist pin + archive state.
            metadata_patch=req.metadata,
            archived=req.archived,
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
        app.state.bus.publish(
            Event(
                type="session.updated",
                session_id=sid,
                payload=Session(**sess.to_wire()).model_dump(exclude_none=True),
            )
        )
        _mirror_workspace_session(app, sid)
        return Session(**sess.to_wire())

    @app.get("/v1/sessions", response_model=ListSessionsResponse)
    async def list_sessions(
        workspace_id: Optional[str] = None,
        include_all_workspaces: bool = False,
        archived: Optional[bool] = None,
    ) -> ListSessionsResponse:
        effective_workspace_id = workspace_id or (None if include_all_workspaces else "ws_default")
        rows = app.state.sessions.list(workspace_id=effective_workspace_id)
        # iowarp/gact-tui §audit/E-14: archive partition. ?archived=true
        # → only archived; ?archived=false (default) → only active. The
        # desktop toggles this through the SessionsColumn archive view.
        if archived is None:
            rows = [r for r in rows if not getattr(r, "archived", False)]
        else:
            rows = [r for r in rows if bool(getattr(r, "archived", False)) == bool(archived)]
        return ListSessionsResponse(sessions=[Session(**row.to_wire()) for row in rows])

    @app.get("/v1/sessions/{sid}", response_model=Session)
    async def get_session(sid: str, workspace_id: Optional[str] = None) -> Session:
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
        if workspace_id and sess.workspace_id != workspace_id:
            raise HTTPException(
                status_code=403,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="permission_error",
                        message="session is outside the requested workspace scope",
                        details={
                            "session_id": sid,
                            "session_workspace_id": sess.workspace_id,
                            "requested_workspace_id": workspace_id,
                            "scope": "other_workspace",
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        return Session(**sess.to_wire())

    # ---- /v1/sessions/{sid}/context/* (ARC live-context plane) -------
    # The session context compartment policy + the live ARC context-plane
    # routes (state/ops/compact/search) are owned by routes/context.py and
    # registered below (after ``deps`` is built); the segment-token arithmetic
    # + window resolution remain in runtime/context_tokens.py (shared with the
    # expert forward path).

    @app.delete("/v1/sessions/{sid}")
    async def delete_session(sid: str) -> Response:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        _guard_direct_destructive_action(
            app,
            session_id=sid,
            workspace_id=sess.workspace_id,
            tool_name="gact.session.delete",
            args={"session_id": sid},
            summary=f"delete session {sid}",
            reason="user_requested_session_delete",
        )
        _remove_workspace_session_mirror(app, sid)
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
        _delete_session_messages(app, sid)
        _delete_session_context_files(app, sid)
        _release_session_arc(app, sid)
        return Response(status_code=204)

    def _reject_rollback_while_active(sid: str, sess: Any) -> None:
        if getattr(sess, "status", "") in {"running", "waiting_permission"}:
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="conflict",
                        message=f"session {sid} cannot be rolled back while {sess.status}",
                        details={"session_id": sid, "status": sess.status},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

    def _publish_rollback_events(
        sid: str,
        *,
        operation: str,
        deleted_ids: list[str],
        session_payload: dict[str, Any],
        target_message_id: str = "",
        include_target: bool = False,
    ) -> None:
        for message_id in deleted_ids:
            app.state.bus.publish(
                Event(
                    type="message.deleted",
                    session_id=sid,
                    payload={
                        "message_id": message_id,
                        "session_id": sid,
                        "operation": operation,
                    },
                )
            )
        app.state.bus.publish(
            Event(
                type=f"session.{operation}",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "deleted_message_ids": deleted_ids,
                    "target_message_id": target_message_id,
                    "include_target": include_target,
                },
            )
        )
        app.state.bus.publish(
            Event(
                type="session.updated",
                session_id=sid,
                payload=session_payload,
            )
        )

    def _commit_rollback(
        sid: str,
        *,
        operation: str,
        kept_messages: list[Message],
        deleted_messages: list[Message],
        target_message_id: str = "",
        include_target: bool = False,
    ) -> dict[str, Any]:
        _replace_session_messages(app, sid, kept_messages)
        deleted_ids = [m.id for m in deleted_messages]
        updated = app.state.sessions.update(
            sid,
            message_count=len(kept_messages),
            status="idle",
            metadata_patch={
                "last_rollback": {
                    "operation": operation,
                    "deleted_message_ids": deleted_ids,
                    "target_message_id": target_message_id,
                    "include_target": include_target,
                    "memory_scope": "gact_visible_transcript_only",
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                }
            },
        )
        if updated is None:
            raise _session_not_found(sid)
        session_payload = Session(**updated.to_wire()).model_dump(exclude_none=True)
        _publish_rollback_events(
            sid,
            operation=operation,
            deleted_ids=deleted_ids,
            session_payload=session_payload,
            target_message_id=target_message_id,
            include_target=include_target,
        )
        return {
            "session_id": sid,
            "operation": operation,
            "deleted_message_ids": deleted_ids,
            "deleted_messages": deleted_ids,
            "reverted_message_ids": deleted_ids,
            "message_count": len(kept_messages),
            "memory_scope": "gact_visible_transcript_only",
            "session": session_payload,
        }

    @app.post("/v1/sessions/{sid}/undo")
    async def undo_session(sid: str, request: Request) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        _reject_rollback_while_active(sid, sess)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        if body is None:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="undo request body must be an object",
                        details={"session_id": sid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        raw_count = body.get("count", body.get("message_count", 1))
        try:
            count = int(raw_count) if isinstance(raw_count, str | int | float) else 1
        except (TypeError, ValueError):
            count = 1
        if count < 1:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="undo count must be at least 1",
                        details={"session_id": sid, "count": raw_count},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        messages = list(app.state.messages.get(sid, []))
        deleted = messages[-count:]
        kept = messages[: max(0, len(messages) - count)]
        _guard_direct_destructive_action(
            app,
            session_id=sid,
            workspace_id=sess.workspace_id,
            tool_name="gact.session.undo",
            args={"session_id": sid, "count": count},
            summary=f"undo last {count} message(s) in session {sid}",
            reason="user_requested_session_undo",
        )
        return _commit_rollback(
            sid,
            operation="undo",
            kept_messages=kept,
            deleted_messages=deleted,
        )

    @app.post("/v1/sessions/{sid}/rewind")
    async def rewind_session(sid: str, request: Request) -> dict[str, Any]:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        _reject_rollback_while_active(sid, sess)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        if not isinstance(body, dict):
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="rewind request body must be an object",
                        details={"session_id": sid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        target_message_id = str(
            body.get("message_id")
            or body.get("target_message_id")
            or body.get("to_message_id")
            or ""
        ).strip()
        if not target_message_id:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="validation_error",
                        message="rewind requires message_id",
                        details={"session_id": sid},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        include_target = bool(body.get("include_target", False))
        messages = list(app.state.messages.get(sid, []))
        target_index = next(
            (index for index, message in enumerate(messages) if message.id == target_message_id),
            -1,
        )
        if target_index < 0:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"message not found: {target_message_id}",
                        details={"session_id": sid, "message_id": target_message_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        keep_end = target_index if include_target else target_index + 1
        kept = messages[:keep_end]
        deleted = messages[keep_end:]
        _guard_direct_destructive_action(
            app,
            session_id=sid,
            workspace_id=sess.workspace_id,
            tool_name="gact.session.rewind",
            args={
                "session_id": sid,
                "message_id": target_message_id,
                "include_target": include_target,
            },
            summary=f"rewind session {sid} to message {target_message_id}",
            reason="user_requested_session_rewind",
        )
        return _commit_rollback(
            sid,
            operation="rewind",
            kept_messages=kept,
            deleted_messages=deleted,
            target_message_id=target_message_id,
            include_target=include_target,
        )

    # ---- /v1/permissions (BBB23) + /v1/policies (SPEC §6.11.b) --------
    # Permission-request ledger CRUD + declarative permission-policy CRUD are
    # owned by routes/permissions.py; registered once below alongside the other
    # register_<concern>_routes factories (after ``deps`` is built).

    # ---- POST /v1/sessions/{sid}/fork (BBB26) -------------------------

    @app.post("/v1/sessions/{sid}/fork")
    async def fork_session(sid: str, request: Request) -> Response:
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
        _replace_session_messages(
            app,
            new_sess.id,
            [m.model_copy(deep=True) for m in src_msgs],
        )
        source_context_files = app.state.context_files.get(sid, {})
        if source_context_files:
            app.state.context_files[new_sess.id] = {
                key: dict(row) for key, row in source_context_files.items()
            }
        app.state.sessions.update(new_sess.id, message_count=len(src_msgs))
        return JSONResponse(
            status_code=201,
            content=Session(**new_sess.to_wire()).model_dump(exclude_none=True),
        )

    # ---- /v1/providers (#15) ------------------------------------------

    def _provider_auth_state(preset: "LMProviderPreset") -> tuple[list[str], bool]:
        """Return (auth_methods, is_authenticated) for a preset.

        Maps CLIO's preset flags to the GACT v0.1 §6.12 Provider shape so
        the TUI's settings picker can render the right state badge:

        - argonne_*: globus oauth; authenticated when tokens are on disk
          AND globus-sdk is importable.
        - cloud (requires_api_key=True): api_key auth; authenticated when
          the matching env var is set.
        - local (lm_studio/ollama/codex): no auth required;
          surface as ``["none"]``, always authenticated.
        """
        if preset.provider == "argonne":
            authed = False
            try:
                from clio_agent.providers import argonne_auth  # noqa: PLC0415

                authed = (
                    argonne_auth.tokens_exist()
                    and importlib.util.find_spec("globus_sdk") is not None
                    and argonne_auth.check_auth_status()
                )
            except Exception:
                authed = False
            return ["oauth"], authed

        if preset.requires_api_key:
            env_var = {
                "anthropic": "ANTHROPIC_API_KEY",
                "openai": "OPENAI_API_KEY",
            }.get(preset.provider, "CLIO_LM_API_KEY")
            return ["api_key"], bool(os.environ.get(env_var) or os.environ.get("CLIO_LM_API_KEY"))

        return ["none"], True

    def _provider_to_wire(preset: "LMProviderPreset") -> dict[str, Any]:
        auth_methods, is_authed = _provider_auth_state(preset)
        return {
            "id": preset.id,
            "name": preset.label,
            "auth_methods": auth_methods,
            "is_authenticated": is_authed,
            "default_model": preset.suggested_model,
            "api_base": preset.api_base,
            "env_keys": (["CLIO_LM_API_KEY"] if preset.requires_api_key else []),
            "description": preset.description,
            "metadata": {
                "provider_kind": preset.provider,
                "requires_api_key": preset.requires_api_key,
                "supports_vision": bool(getattr(preset, "supports_vision", False)),
            },
        }

    @app.get("/v1/providers")
    async def list_providers() -> dict[str, Any]:
        """SPEC §6.12 — generic LM provider catalog.

        Returns one row per preset with the v0.1 fields (id, name,
        auth_methods, is_authenticated, default_model) so the TUI's
        settings picker can render the right state badge per provider
        and decide whether to surface a "Login" affordance.
        """

        return {"providers": [_provider_to_wire(p) for p in _LM_PRESETS]}

    # GET /v1/providers/{provider_id} is registered after the literal
    # /v1/providers/lm route so the LM configuration endpoint keeps
    # winning FastAPI's order-based route match.

    @app.post("/v1/providers/{provider_id}/auth")
    async def auth_provider(provider_id: str, request: Request) -> dict[str, Any]:
        """SPEC §6.12 — kick off provider-specific auth.

        For argonne_*, this launches the Globus OAuth flow in an
        interactive terminal where the user can visit the URL and
        paste the generated code. This endpoint must not validate or
        refresh cached tokens inline: expired Globus sessions can
        block waiting for terminal input, which would freeze the TUI
        request instead of giving the user an actionable login path.

        Other providers (cloud / local) use api_key / no-auth and
        return 405 with a hint pointing to PUT /v1/providers/lm.
        """

        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"unknown provider: {provider_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        if preset.provider != "argonne":
            raise HTTPException(
                status_code=405,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="unsupported",
                        message=(
                            f"provider '{provider_id}' uses "
                            f"{'api_key' if preset.requires_api_key else 'no'} "
                            "auth; pass api_key directly to PUT /v1/providers/lm."
                        ),
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )

        if importlib.util.find_spec("globus_sdk") is None:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="dependency_missing",
                        message=(
                            "globus-sdk not installed. Install with "
                            "'pip install clio-agent[argonne]' on the "
                            "backend host and retry."
                        ),
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        force = bool(body.get("force", False))

        command = [
            sys.executable,
            "-m",
            "clio_agent.providers.argonne_auth",
            "authenticate",
        ]
        if force:
            command.append("--force")
        manual_command = " ".join(command)
        try:
            if os.name == "nt":
                powershell = (
                    shutil.which("pwsh.exe") or shutil.which("powershell.exe") or "powershell.exe"
                )
                command_literal = " ".join(
                    f"'{part.replace(chr(39), chr(39) + chr(39))}'" for part in command
                )
                ps_script = (
                    "$Host.UI.RawUI.WindowTitle = 'CLIO ALCF Globus Login'; "
                    "Write-Host 'CLIO ALCF Globus login'; "
                    f"Write-Host 'Running: {manual_command.replace(chr(39), chr(39) + chr(39))}'; "
                    "Write-Host ''; "
                    f"& {command_literal}; "
                    "$exitCode = $LASTEXITCODE; "
                    "Write-Host ''; "
                    "Write-Host ('Auth helper exited with code ' + $exitCode); "
                    "Read-Host 'Press Enter to close this window'"
                )
                subprocess.Popen(  # noqa: S603
                    [
                        powershell,
                        "-NoExit",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        ps_script,
                    ],
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                )
                instructions = (
                    "Opened a persistent PowerShell window for ALCF Globus login. Complete the "
                    "authorization code flow there, then press Ctrl+R here to refresh provider status. "
                    f"If no terminal appears, run: {manual_command}"
                )
            else:
                terminal = next(
                    (
                        shutil.which(name)
                        for name in ("x-terminal-emulator", "gnome-terminal", "konsole", "xterm")
                        if shutil.which(name)
                    ),
                    None,
                )
                if terminal:
                    term_name = os.path.basename(terminal)
                    args = (
                        [terminal, "--", *command]
                        if term_name == "gnome-terminal"
                        else [terminal, "-e", *command]
                    )
                    subprocess.Popen(args)  # noqa: S603
                    instructions = (
                        "Opened a terminal for ALCF Globus login. Complete the "
                        "authorization code flow there, then press Ctrl+R here to refresh provider status. "
                        f"If no terminal appears, run: {manual_command}"
                    )
                else:
                    instructions = (
                        "Run this in an interactive terminal, then press Ctrl+R here: "
                        + manual_command
                    )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="argonne_auth_failed",
                        message=f"Could not launch interactive Globus authentication: {exc}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc

        return {
            "is_authenticated": False,
            "provider_id": provider_id,
            "instructions": instructions,
        }

    # Per-provider model catalogs. Hand-curated rather than introspected
    # because most upstreams either don't expose a /models endpoint or
    # return hundreds of irrelevant entries. The TUI's Settings → Model
    # picker calls this once per provider and lists the rows verbatim.
    # Derived from clio_agent.providers.registry. Static fallback used
    # only when live model discovery against the upstream /v1/models
    # endpoint fails (no key, network down, 5xx) — see the GET
    # /v1/providers/{id}/models handler below for the resolution order.
    # ALCF / Argonne live model availability is dynamic (jobs spin up
    # and tear down behind the gateway); the live set can be queried
    # with `scripts/list_active_models.sh` in alcf-agentics-workflow.
    from clio_agent.providers.registry import (
        as_provider_models_dict as _build_provider_models,
    )

    _PROVIDER_MODELS: dict[str, list[dict[str, str]]] = _build_provider_models()

    @app.get("/v1/providers/{provider_id}/models")
    async def list_provider_models(provider_id: str, api_base: str = "") -> dict[str, Any]:
        """Per-provider model catalog via the unified async handshake.

        Resolves the preset (by id, then by bare kind) and runs the per-provider
        handshake (passive auth — browsing never triggers interactive OAuth),
        returning its ``to_models_wire`` shape with the discovered context windows
        and capability flags. Cached for the handshake TTL so spamming the picker
        doesn't hammer the upstream.

        A live provider that fails reports ``source="unavailable"`` with the reason
        rather than stale static choices; CLI providers (codex/claude_code) expose
        an editable static candidate catalog; unknown provider ids return a 404.
        """

        # Resolve the preset (by id, then by bare kind) and run the *unified*
        # handshake — provider-agnostic; the per-provider handshake class owns
        # the protocol details. Passive auth: browsing the picker must never
        # trigger an interactive OAuth flow.
        import os as _os  # noqa: PLC0415

        from clio_agent.providers.handshake import (  # noqa: PLC0415
            HandshakeContext,
            run_handshake,
        )
        from clio_agent.providers.registry import (  # noqa: PLC0415
            as_cloud_api_key_env as _cloud_env,
        )

        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            preset = next((p for p in _LM_PRESETS if p.provider == provider_id), None)
        if preset is None:
            # Last-ditch static for known provider ids only.
            models = _PROVIDER_MODELS.get(provider_id)
            if models is None:
                raise HTTPException(
                    status_code=404,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="not_found",
                            message=f"unknown provider: {provider_id}",
                            details={"available": sorted(_PROVIDER_MODELS)},
                            recoverable=False,
                        )
                    ).model_dump(exclude_none=True),
                )
            return {"models": models, "source": "static_catalog"}

        env_name = _cloud_env().get(preset.provider, "")
        api_key = (
            _os.environ.get(env_name, "") if env_name else _os.environ.get("CLIO_LM_API_KEY", "")
        )
        ctx = HandshakeContext(
            provider_id=preset.id,
            provider_kind=preset.provider,
            api_base=(api_base or preset.api_base or ""),
            api_key=api_key,
            auth_mode="passive",
            allow_external_sources=True,
        )
        report = await run_handshake(ctx)
        wire = report.to_models_wire()
        # CLI providers (codex / claude_code) have no live ``/models`` endpoint, so
        # they expose an editable static candidate catalog. A *live* provider that
        # failed reports ``unavailable`` + the reason rather than showing stale
        # static choices — surfacing the problem, never silently lying with a cache.
        if not wire.get("models") and preset.provider in {"codex", "claude_code"}:
            static = _PROVIDER_MODELS.get(preset.id) or _PROVIDER_MODELS.get(preset.provider)
            if static:
                return {"models": static, "source": "static_catalog"}
        return wire

    @app.get("/v1/providers/{provider_id}/handshake")
    async def provider_handshake(
        provider_id: str, api_base: str = "", refresh: bool = False
    ) -> dict[str, Any]:
        """Async provider handshake: connectivity + auth + per-model config.

        Report-only (no runtime mutation). Runs the per-provider handshake and
        returns the discovered context windows, reasoning/tool capabilities and
        provenance alongside the legacy model list (``to_models_wire`` shape).
        Cached for the handshake TTL; ``refresh=true`` forces a re-probe. Argonne
        resolves its own stored token (passive, never interactive).
        """
        import os as _os  # noqa: PLC0415

        from clio_agent.providers.handshake import (  # noqa: PLC0415
            HandshakeContext,
            run_handshake,
        )
        from clio_agent.providers.registry import (  # noqa: PLC0415
            as_cloud_api_key_env as _cloud_env,
        )

        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            preset = next((p for p in _LM_PRESETS if p.provider == provider_id), None)
        if preset is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"unknown provider: {provider_id}",
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        env_name = _cloud_env().get(preset.provider, "")
        api_key = (
            _os.environ.get(env_name, "") if env_name else _os.environ.get("CLIO_LM_API_KEY", "")
        )
        ctx = HandshakeContext(
            provider_id=preset.id,
            provider_kind=preset.provider,
            api_base=(api_base or preset.api_base or ""),
            api_key=api_key,
            auth_mode="passive",
            allow_external_sources=True,
        )
        report = await run_handshake(ctx, force=refresh)
        out = report.to_models_wire()
        out["connectivity"] = report.connectivity.value
        out["auth"] = report.auth.value
        out["latency_ms"] = report.latency_ms
        out["generated_at"] = report.generated_at
        return out

    # ---- /v1/mcp/servers (#13) ---------------------------------------
    # The MCP server registry + dispatch routes (servers list/detail/install/
    # call/reconnect/uninstall + tools/resources/prompts + handshake) are owned
    # by routes/mcp.py; registered below via register_mcp_routes(app, deps).

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
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"session not found: {sid}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        ledger = app.state.messages.get(sid, [])
        if not ledger:
            return {
                "session_id": sid,
                "compacted": False,
                "reason": "session has no messages to compact",
            }

        # Build a transcript blob. Keep enough per-part evidence for scientific
        # identifiers and metrics to survive compaction, while still bounding
        # pathological tool output.
        # ledger entries are Pydantic Message models (see types.py); use
        # attribute access + model_dump() defensively for dict-shaped
        # entries the older code paths still produce.
        def _attr(o, name, default=None):
            if hasattr(o, name):
                return getattr(o, name)
            if isinstance(o, dict):
                return o.get(name, default)
            return default

        per_part_limit = 6000
        transcript_limit = 60000
        chunks: list[str] = []
        transcript_chars = 0
        for m in ledger[-50:]:  # last 50 messages should be enough context
            role = (_attr(m, "role", "user") or "user").upper()
            for p in _attr(m, "parts", []) or []:
                txt = _attr(p, "text", "") or ""
                if len(txt) > per_part_limit:
                    head_limit = per_part_limit // 2
                    tail_limit = per_part_limit - head_limit
                    txt = (
                        txt[:head_limit]
                        + "\n[...part truncated for compaction...]\n"
                        + txt[-tail_limit:]
                    )
                txt = txt.strip()
                if not txt:
                    continue
                chunk = f"{role}: {txt}"
                remaining = transcript_limit - transcript_chars
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunk = chunk[:remaining] + "\n[...transcript truncated for compaction...]"
                chunks.append(chunk)
                transcript_chars += len(chunk)
            if transcript_chars >= transcript_limit:
                break
        transcript = "\n".join(chunks)
        if not transcript.strip():
            return {
                "session_id": sid,
                "compacted": False,
                "reason": "transcript is empty after part filtering",
            }

        agent = app.state.agent
        if agent is None:
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="agent_unavailable",
                        message="no LM agent wired; configure one via PUT /v1/providers/lm",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        # Try to extract optional focus instructions from the body.
        try:
            body = await request.json()
        except Exception:
            body = {}
        focus = (body.get("focus") or "").strip()

        prompt = (
            "Create an evidence-preserving compact memory for the following CLIO "
            "conversation transcript. This memory will replace the archived transcript, "
            "so preserve concrete scientific evidence, not just a high-level story.\n\n"
            "Rules:\n"
            "- Keep exact file paths, dataset names, column names, variable names, "
            "units, dimensions, counts, statistics, artifact paths, and error messages "
            "when they appear in the transcript.\n"
            "- Preserve which findings came from which source, grouped by file/provider "
            "or workflow stage.\n"
            "- Preserve unresolved gaps, failed inspections, missing dependencies, and "
            "next checks.\n"
            "- If evidence is missing or a source was not inspected, say that explicitly. "
            "Do not fill gaps with plausible details.\n"
            "- Do not invent dataset names, columns, statistics, compression settings, "
            "or readiness conclusions that are not supported by the transcript.\n"
            "- Prefer concise structured bullets over prose. Keep the summary compact, "
            "but do not omit identifiers needed for a later expert to continue the work."
        )
        if focus:
            prompt += f"\n\nFocus the summary on: {focus}"
        prompt += f"\n\n--- transcript ---\n{transcript}\n--- end ---"

        def _summarize_with_provider_retries() -> str:
            def summarize() -> str:
                return agent._run_chat_agent(prompt, "")

            retry_call = getattr(agent, "_call_with_transient_provider_retries", None)
            if callable(retry_call):
                return retry_call("compact_summary", summarize)
            return summarize()

        try:
            summary = await asyncio.get_running_loop().run_in_executor(
                None,
                _summarize_with_provider_retries,
            )
            evidence_index = _compact_exact_evidence_index(transcript)
            if evidence_index:
                summary = (summary or "").rstrip() + "\n\n" + evidence_index
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="upstream_error",
                        message=f"compact summarisation failed: {exc!r}",
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            ) from exc

        # Insert the summary as a new assistant message at the head of the
        # ledger (after archiving the originals to a parallel list so a
        # future /resume can recover full history). The TUI doesn't see
        # archived messages — only the compact summary + anything that
        # comes after it.
        event_id = _new_memory_event_id()
        compacted_at = datetime.now(timezone.utc).isoformat()
        archive = app.state.__dict__.setdefault("session_archives", {})
        archive.setdefault(sid, []).append(
            {
                "compacted_at": time.time(),
                "memory_event_id": event_id,
                "messages": list(ledger),
            }
        )

        arc = getattr(agent, "arc", None)
        arc_status = "not_configured"
        if arc is not None:
            try:
                from clio_agent.arc.schema import (  # noqa: PLC0415
                    Conversation as ARCConversation,
                )
                from clio_agent.arc.schema import Message as ARCMessage  # noqa: PLC0415

                now_ts = time.time()
                arc_summary = ARCMessage(
                    role="assistant",
                    content="[compact summary]\n" + (summary or "").strip(),
                    timestamp=now_ts,
                    metadata={
                        "source": "gact_compact",
                        "synthetic": "compact_summary",
                        "memory_event_id": event_id,
                        "archived_count": len(ledger),
                    },
                )
                conv = arc.get_conversation(sid)
                if conv is None:
                    conv = ARCConversation(
                        session_id=sid,
                        user_id="default_user",
                        created_at=now_ts,
                        updated_at=now_ts,
                        last_accessed=now_ts,
                        status="active",
                        messages=[arc_summary],
                        routing_decisions=[],
                        metadata={
                            "clio_agent_version": "0.2.0",
                            "arc_enabled": True,
                            "compacted_by": "gact",
                        },
                        storage_tier="warm",
                    )
                else:
                    conv.messages = [arc_summary]
                    conv.updated_at = now_ts
                    conv.last_accessed = now_ts
                    conv.metadata["compacted_by"] = "gact"
                    conv.metadata["compacted_at"] = now_ts
                    conv.metadata["archived_message_count"] = len(ledger)
                arc.store_conversation(conv)
                arc_status = "stored"
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=500,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="memory_update_failed",
                            message=f"compact summary could not be stored in ARC memory: {exc!r}",
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                ) from exc

        from clio_agent.gact.types import Message, Part, Tokens  # noqa: PLC0415

        compact_message = Message(
            id=f"msg_compact_{uuid.uuid4().hex[:10]}",
            turn_id=_active_semantic_turn_id(),
            session_id=sid,
            role="assistant",
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat(),
            parts=[
                Part(
                    id=f"part_compact_{uuid.uuid4().hex[:10]}",
                    type="text",
                    metadata={
                        "synthetic": "compact_summary",
                        "memory_event_id": event_id,
                    },
                    text="[compact summary]\n" + (summary or "").strip(),
                )
            ],
            tokens=Tokens(input=0, output=0, cache_read=0, cache_write=0),
            cost_usd=0.0,
            stop_reason="end_turn",
            metadata={
                "synthetic": "compact_summary",
                "memory_event_id": event_id,
            },
        )
        _replace_session_messages(app, sid, [compact_message])
        memory_event = {
            "id": event_id,
            "version": 1,
            "type": "compact_summary",
            "session_id": sid,
            "created_at": compacted_at,
            "updated_at": compacted_at,
            "summary_message_id": compact_message.id,
            "archived_count": len(ledger),
            "summary_chars": len((summary or "")),
            "transcript_chars": len(transcript),
            "transcript_limit": transcript_limit,
            "per_part_limit": per_part_limit,
            "focus": focus,
            "arc_status": arc_status,
            "metadata": {
                "source": "gact_compact",
                "synthetic": "compact_summary",
                "evidence_index": "[exact retained evidence index]" in (summary or ""),
            },
        }
        app.state.memory_events.setdefault(sid, []).append(memory_event)
        _emit_semantic_event(
            app,
            sid,
            "memory.compacted",
            turn_id=_ctx.active_turn_id(),
            trace_id=_ctx.active_trace_id(),
            summary="Session transcript was compacted into memory.",
            actor={"role": "runtime", "component": "memory"},
            subject={"memory_event_id": event_id},
            payload=memory_event,
        )

        # Publish so any open SSE stream redraws.
        app.state.bus.publish(
            Event(
                type="session.compacted",
                session_id=sid,
                payload={
                    "event_id": event_id,
                    "archived_count": len(ledger),
                    "summary_chars": len((summary or "")),
                    "summary_message_id": compact_message.id,
                    "version": 1,
                },
            )
        )
        return {
            "session_id": sid,
            "compacted": True,
            "event_id": event_id,
            "archived_count": len(ledger),
            "summary": summary,
        }

    # ---- /v1/sessions/{sid}/schedules (#21) --------------------------
    # Scheduled-turn CRUD (list/add/delete) is owned by routes/schedules.py and
    # registered below via register_schedules_routes(app, deps) once ``deps`` is
    # built; the scheduler tick task (above) owns the actual firing.

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
            "workspace": (Workspace(**ws.to_wire()).model_dump(exclude_none=True) if ws else None),
            "messages": [m.model_dump(exclude_none=True) for m in msgs],
            "context_files": [dict(row) for row in app.state.context_files.get(sid, {}).values()],
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
        if blob.get("workspace") and app.state.workspaces.get(blob["workspace"].get("id", "")):
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
        _replace_session_messages(app, new_sess.id, msg_rows)
        context_files: dict[str, dict[str, Any]] = {}
        for row in blob.get("context_files", []):
            if not isinstance(row, Mapping):
                continue
            path = str(row.get("path") or "").strip()
            if not path:
                continue
            context_files[path] = dict(row)
        if context_files:
            app.state.context_files[new_sess.id] = context_files
        cost_total = sum(float(m.get("cost_usd", 0.0) or 0.0) for m in blob.get("messages", []))
        in_total = sum(
            int((m.get("tokens") or {}).get("input", 0) or 0) for m in blob.get("messages", [])
        )
        out_total = sum(
            int((m.get("tokens") or {}).get("output", 0) or 0) for m in blob.get("messages", [])
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
                matches.append(
                    {
                        "message_id": m.id,
                        "part_id": part.id,
                        "snippet": snippet,
                        "score": 1.0 + (idx * 0.01),
                    }
                )
        matches.sort(key=lambda r: r["score"], reverse=True)
        return {"matches": matches}

    # ---- Ask-user and retry protocol (#333) --------------------------

    def _session_not_found(sid: str) -> HTTPException:
        return HTTPException(
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

    def _question_not_found(sid: str, question_id: str) -> HTTPException:
        return HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"user question not found: {question_id}",
                    details={"session_id": sid, "question_id": question_id},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    def _pending_user_questions(sid: str) -> list[UserQuestion]:
        return [
            q
            for q in app.state.user_questions.values()
            if q.session_id == sid and q.status == "pending"
        ]

    def _set_session_status(
        sid: str,
        status: str,
        *,
        prev_status: str = "",
        metadata_patch: Optional[dict[str, Any]] = None,
    ) -> None:
        updated = app.state.sessions.update(
            sid,
            status=status,
            metadata_patch=metadata_patch,
        )
        app.state.bus.publish(
            Event(
                type="session.status_changed",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "status": status,
                    "prev_status": prev_status,
                    "updated_at": updated.updated_at if updated is not None else "",
                },
            )
        )

    def _normalize_question_options(
        req: CreateUserQuestionRequest,
    ) -> list[UserQuestionOption]:
        if req.kind == "confirmation" and not req.options:
            return [
                UserQuestionOption(label="Yes", value="yes", description=""),
                UserQuestionOption(label="No", value="no", description=""),
            ]
        return list(req.options)

    def _start_background_user_turn(
        sid: str,
        sess: Session,
        user_text: str,
        *,
        request_parts: Optional[list[Part]] = None,
        metadata: Optional[dict[str, Any]] = None,
        prev_status: str = "idle",
        turn_agent_id: str = "",
    ) -> Message:
        now = time.time()
        user_metadata = dict(metadata or {})
        user_parts = _user_message_parts(
            request_parts=list(request_parts or []),
            user_text=user_text,
        )
        image_count = sum(1 for part in user_parts if part.type == "image")
        if image_count:
            native_dispatch = _agent_accepts_images(app.state.agent)
            user_metadata["multimodal"] = {
                "image_part_count": image_count,
                "transcript_preserved": True,
                "native_model_dispatch": native_dispatch,
            }
            user_metadata["image_parts"] = _image_part_summaries(user_parts)
        if turn_agent_id:
            user_metadata["agent_override"] = {
                "requested_agent_id": turn_agent_id,
                "session_agent_id": _session_agent_id(sess),
                "scope": "turn",
            }
        user_msg_id = _new_message_id("user")
        user_msg = Message(
            id=user_msg_id,
            # The turn id IS the user message id (#711); a user message correlates to
            # its own turn.
            turn_id=user_msg_id,
            session_id=sid,
            role="user",
            created_at=_iso_from_epoch(now),
            updated_at=_iso_from_epoch(now),
            parts=user_parts,
            metadata=user_metadata,
        )

        _append_session_message(app, sid, user_msg)
        app.state.sessions.update(sid, status="running")
        app.state.bus.publish(
            Event(
                type="session.status_changed",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "status": "running",
                    "prev_status": prev_status,
                },
            )
        )
        app.state.bus.publish(
            Event(
                type="message.created",
                session_id=sid,
                payload=user_msg.model_dump(exclude_none=True),
            )
        )

        task = asyncio.create_task(
            _run_turn_in_background(app, sid, user_text, user_msg, turn_agent_id)
        )
        app.state.in_flight_turns[sid] = task

        def _drop_task(_t, _sid=sid) -> None:
            cur = app.state.in_flight_turns.get(_sid)
            if cur is _t:
                app.state.in_flight_turns.pop(_sid, None)

        task.add_done_callback(_drop_task)
        return user_msg

    def _message_text(message: Message) -> str:
        return "\n".join(
            part.text for part in message.parts if part.type == "text" and part.text
        ).strip()

    def _retry_source_user_message(messages: list[Message], source: Message) -> Message | None:
        if source.role == "user":
            return source
        try:
            source_index = next(idx for idx, msg in enumerate(messages) if msg.id == source.id)
        except StopIteration:
            return None
        for msg in reversed(messages[:source_index]):
            if msg.role == "user":
                return msg
        return None

    def _retry_user_text(original_text: str, notes: str) -> str:
        notes = notes.strip()
        if not notes:
            return original_text
        return f"{original_text}\n\n[Retry notes]\n{notes}"

    @app.get("/v1/sessions/{sid}/questions")
    async def list_user_questions(sid: str, status: str = "") -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        rows = [q for q in app.state.user_questions.values() if q.session_id == sid]
        if status:
            rows = [q for q in rows if q.status == status]
        rows.sort(key=lambda q: q.created_at, reverse=True)
        return {"questions": [q.model_dump(exclude_none=True) for q in rows]}

    @app.post("/v1/sessions/{sid}/questions", response_model=UserQuestion, status_code=201)
    async def create_user_question(
        sid: str,
        req: CreateUserQuestionRequest,
    ) -> UserQuestion:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        prompt = req.prompt.strip()
        if not prompt:
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="missing required field: prompt",
                        details={"field": "prompt"},
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        now_iso = datetime.now(timezone.utc).isoformat()
        row = UserQuestion(
            id=_new_question_id(),
            session_id=sid,
            prompt=prompt,
            kind=req.kind,
            options=_normalize_question_options(req),
            created_at=now_iso,
            updated_at=now_iso,
            expires_at=req.expires_at,
            source=req.source or "orchestrator",
            turn_id=req.turn_id,
            attempt_id=req.attempt_id,
            metadata=req.metadata,
        )
        app.state.user_questions[row.id] = row
        _set_session_status(
            sid,
            "waiting_user",
            prev_status=sess.status,
            metadata_patch={"pending_user_question_id": row.id},
        )
        app.state.bus.publish(
            Event(
                type="user_question.created",
                session_id=sid,
                payload=row.model_dump(exclude_none=True),
            )
        )
        return row

    @app.post("/v1/sessions/{sid}/questions/{question_id}/answer", response_model=UserQuestion)
    async def answer_user_question(
        sid: str,
        question_id: str,
        req: AnswerUserQuestionRequest,
    ) -> UserQuestion:
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        row = app.state.user_questions.get(question_id)
        if row is None or row.session_id != sid:
            raise _question_not_found(sid, question_id)
        if row.status != "pending":
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message=f"user question is already {row.status}",
                        details={"session_id": sid, "question_id": question_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        allowed_values = {o.value or o.label for o in row.options}
        selected = [s for s in req.selected_options if s]
        if allowed_values and selected and any(s not in allowed_values for s in selected):
            raise HTTPException(
                status_code=422,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="bad_request",
                        message="selected option is not valid for this question",
                        details={
                            "session_id": sid,
                            "question_id": question_id,
                            "allowed": sorted(allowed_values),
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        updated = row.model_copy(
            update={
                "status": "answered",
                "answer": req.answer,
                "selected_options": selected,
                "answer_metadata": req.metadata,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        app.state.user_questions[question_id] = updated
        if not _pending_user_questions(sid):
            sess = app.state.sessions.get(sid)
            should_resume = bool(updated.metadata.get("resume_on_answer")) and sess is not None
            if should_resume and app.state.agent is not None:
                app.state.sessions.update(
                    sid,
                    metadata_patch={"pending_user_question_id": ""},
                )
                resumed_msg = _start_background_user_turn(
                    sid,
                    sess,
                    _ask_user_resume_text(updated),
                    metadata={
                        "ask_user_question_id": updated.id,
                        "ask_user_prompt": updated.prompt,
                        "ask_user_answer": updated.answer,
                        "ask_user_selected_options": updated.selected_options,
                        "ask_user_source_turn_id": updated.turn_id,
                        "ask_user_attempt_id": updated.attempt_id,
                        "ask_user_caller": updated.metadata.get("caller", {}),
                        "ask_user_resume": True,
                    },
                    prev_status=sess.status if sess is not None else "waiting_user",
                )
                app.state.bus.publish(
                    Event(
                        type="user_question.resumed",
                        session_id=sid,
                        payload={
                            "question_id": updated.id,
                            "session_id": sid,
                            "queued_user_message_id": resumed_msg.id,
                            "source_turn_id": updated.turn_id,
                        },
                    )
                )
            else:
                _set_session_status(
                    sid,
                    "idle",
                    prev_status=sess.status if sess is not None else "waiting_user",
                    metadata_patch={"pending_user_question_id": ""},
                )
        app.state.bus.publish(
            Event(
                type="user_question.answered",
                session_id=sid,
                payload=updated.model_dump(exclude_none=True),
            )
        )
        return updated

    @app.post("/v1/sessions/{sid}/questions/{question_id}/cancel", response_model=UserQuestion)
    async def cancel_user_question(sid: str, question_id: str) -> UserQuestion:
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        row = app.state.user_questions.get(question_id)
        if row is None or row.session_id != sid:
            raise _question_not_found(sid, question_id)
        if row.status == "pending":
            row = row.model_copy(
                update={
                    "status": "cancelled",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            app.state.user_questions[question_id] = row
        if not _pending_user_questions(sid):
            sess = app.state.sessions.get(sid)
            _set_session_status(
                sid,
                "idle",
                prev_status=sess.status if sess is not None else "waiting_user",
                metadata_patch={"pending_user_question_id": ""},
            )
        app.state.bus.publish(
            Event(
                type="user_question.cancelled",
                session_id=sid,
                payload=row.model_dump(exclude_none=True),
            )
        )
        return row

    @app.get("/v1/sessions/{sid}/attempts")
    async def list_turn_attempts(sid: str) -> dict[str, Any]:
        if app.state.sessions.get(sid) is None:
            raise _session_not_found(sid)
        rows = [a for a in app.state.turn_attempts.values() if a.session_id == sid]
        rows.sort(key=lambda a: a.created_at, reverse=True)
        return {"attempts": [a.model_dump(exclude_none=True) for a in rows]}

    @app.post(
        "/v1/sessions/{sid}/messages/{message_id}/retry",
        response_model=TurnAttempt,
        status_code=202,
    )
    async def retry_turn(sid: str, message_id: str, req: RetryTurnRequest) -> TurnAttempt:
        sess = app.state.sessions.get(sid)
        if sess is None:
            raise _session_not_found(sid)
        messages = app.state.messages.get(sid, [])
        source = next((m for m in messages if m.id == message_id), None)
        if source is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"message not found: {message_id}",
                        details={"session_id": sid, "message_id": message_id},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        model_payload = (req.model or ModelRef()).model_dump()
        if req.provider_id:
            model_payload["provider_id"] = req.provider_id
        if req.model_id:
            model_payload["model_id"] = req.model_id
        active_model = _active_lm_model_ref(app)
        model_changed = bool(
            (model_payload.get("provider_id") or model_payload.get("model_id"))
            and (
                model_payload.get("provider_id", "") != active_model.get("provider_id", "")
                or model_payload.get("model_id", "") != active_model.get("model_id", "")
            )
        )
        warning = ""
        if model_changed:
            warning = (
                "Retrying with a different model/provider may recompute provider-side KV "
                "cache, increase time to first token, increase latency/cost, and produce "
                "different tool or reasoning behavior."
            )
        execution_blocked_reason = ""
        retry_user_msg: Message | None = None
        source_user = _retry_source_user_message(messages, source)
        if req.execute:
            if app.state.agent is None:
                raise HTTPException(
                    status_code=503,
                    detail=_agent_not_available_error(app, sid).model_dump(exclude_none=True),
                )
            lm_status = getattr(app.state, "lm_config_status", {}) or {}
            if lm_status.get("state") == "configuring":
                raise HTTPException(
                    status_code=503,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="provider_configuring",
                            message=(
                                "LM provider configuration is still in progress; retry after "
                                "it finishes."
                            ),
                            details={
                                "session_id": sid,
                                "operation_id": lm_status.get("operation_id", ""),
                                "provider": lm_status.get("provider", ""),
                                "model": lm_status.get("model", ""),
                                "recovery_actions": [
                                    "wait",
                                    "check_lm_provider_status",
                                    "retry",
                                ],
                            },
                            recoverable=True,
                        )
                    ).model_dump(exclude_none=True),
                )
            if source_user is None or not _message_text(source_user):
                execution_blocked_reason = "source_user_message_not_found"
            elif model_changed:
                envelope = _unsupported_model_ref_error(
                    session_id=sid,
                    source="retry",
                    model_ref=model_payload,
                    active_model=active_model,
                )
                envelope.error.details.update(
                    {
                        "message_id": message_id,
                        "notes_present": bool(req.notes.strip()),
                        "warning": warning,
                        "recovery_actions": [
                            "put_global_lm_provider",
                            "retry_without_model_override",
                            "retry_after_provider_switch",
                            "exit",
                        ],
                    }
                )
                raise HTTPException(
                    status_code=422,
                    detail=envelope.model_dump(exclude_none=True),
                )
        now_iso = datetime.now(timezone.utc).isoformat()
        attempt = TurnAttempt(
            id=_new_attempt_id(),
            session_id=sid,
            source_message_id=message_id,
            status=(
                "queued"
                if req.execute and not execution_blocked_reason
                else ("failed" if req.execute else "recorded")
            ),
            created_at=now_iso,
            updated_at=now_iso,
            notes=req.notes,
            model=ModelRef(**model_payload),
            warning=warning,
            metadata={
                **req.metadata,
                "source_message_role": source.role,
                "source_user_message_id": source_user.id if source_user is not None else "",
                "active_model": active_model,
                "retry_protocol": "queued_for_execution" if req.execute else "recorded_for_replay",
                "execution_blocked_reason": execution_blocked_reason,
            },
        )
        app.state.turn_attempts[attempt.id] = attempt
        if req.execute and not execution_blocked_reason and source_user is not None:
            retry_text = _retry_user_text(_message_text(source_user), req.notes)
            retry_user_msg = _start_background_user_turn(
                sid,
                sess,
                retry_text,
                metadata={
                    "retry_attempt_id": attempt.id,
                    "retry_source_message_id": message_id,
                    "retry_source_user_message_id": source_user.id,
                    "retry_notes": req.notes,
                    **req.metadata,
                },
                prev_status=sess.status,
            )
            attempt = attempt.model_copy(
                update={
                    "metadata": {
                        **attempt.metadata,
                        "queued_user_message_id": retry_user_msg.id,
                    }
                }
            )
            app.state.turn_attempts[attempt.id] = attempt
        app.state.bus.publish(
            Event(
                type="turn.retry_requested",
                session_id=sid,
                payload=attempt.model_dump(exclude_none=True),
            )
        )
        return attempt

    # ---- POST /v1/sessions/{sid}/cancel (BBB20) -----------------------

    @app.post("/v1/sessions/{sid}/cancel")
    async def cancel_session(sid: str) -> Response:
        """Best-effort cancel of an in-flight turn on this session.

        The agent loop and sync MCP bridge observe a scoped cancellation
        checker between planner/expert/tool boundaries and return early
        with ``error_info.error == "cancelled"`` when possible. The
        endpoint itself flips the flag + publishes a
        ``session.cancelled`` event so any live SSE subscriber sees
        the transition without waiting for the next turn boundary.

        If the turn is already blocked inside executor-thread provider
        or tool work, cancelling the asyncio Task settles the GACT
        envelope as cancelled but cannot kill the underlying Python
        thread. The emitted status event marks this as best-effort so
        clients do not mistake it for a guaranteed provider abort.

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

        # Set the cancellation flag. Cooperative agent/tool paths check
        # it between expensive boundaries; the turn handler also checks
        # it after forward() returns so non-cooperative agents still
        # produce a truthful cancelled envelope.
        app.state.cancel_flags.add(sid)
        event = app.state.cancel_events.get(sid)
        if event is not None:
            event.set()
        in_flight = app.state.in_flight_turns.get(sid)
        cancellation_pending = False
        if in_flight is not None and not in_flight.done():
            cancellation_pending = True
        attempt = {
            "id": _new_cancellation_attempt_id(),
            "session_id": sid,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "in_flight": cancellation_pending,
            "cooperative_signal_sent": event is not None,
            "asyncio_task_cancel_scheduled": cancellation_pending,
            "asyncio_task_cancel_sent": False,
            "hard_abort_supported": False,
            "upstream_abort": "not_supported",
            "executor_work_may_continue": cancellation_pending,
        }
        app.state.cancel_attempts[sid] = attempt
        if cancellation_pending:

            async def _cancel_after_grace(task: asyncio.Task, session_id: str) -> None:
                await asyncio.sleep(0.1)
                if session_id in app.state.cancel_flags and not task.done():
                    latest_attempt = app.state.cancel_attempts.get(session_id)
                    if latest_attempt is attempt:
                        attempt["asyncio_task_cancel_sent"] = True
                        attempt["asyncio_task_cancelled_at"] = datetime.now(
                            timezone.utc
                        ).isoformat()
                    task.cancel()

            asyncio.create_task(_cancel_after_grace(in_flight, sid))
        app.state.sessions.update(sid, status="cancelled")
        app.state.bus.publish(
            Event(
                type="session.status_changed",
                session_id=sid,
                payload={
                    "session_id": sid,
                    "status": "cancelled",
                    "prev_status": sess.status,
                    "execution_cancellation": (
                        "cooperative_pending" if cancellation_pending else "none"
                    ),
                    "executor_work_may_continue": cancellation_pending,
                    "cancellation_attempt": _cancellation_attempt_summary(attempt),
                },
            )
        )
        return Response(status_code=204)

    # ---- POST /v1/sessions/{sid}/messages (BBB9) ---------------------
    # Non-streaming turn: 1 request, 1 response body containing both
    # the stored user message + the assistant's reply. Streaming
    # (SSE on /v1/sessions/{sid}/events) lands in BBB10.

    @app.post("/v1/sessions/{sid}/messages", response_model=PostMessageResponse)
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
        lm_status = getattr(app.state, "lm_config_status", {}) or {}
        if lm_status.get("state") == "configuring":
            raise HTTPException(
                status_code=503,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="provider_configuring",
                        message=(
                            "LM provider configuration is still in progress; retry after it "
                            "finishes."
                        ),
                        details={
                            "session_id": sid,
                            "operation_id": lm_status.get("operation_id", ""),
                            "provider": lm_status.get("provider", ""),
                            "model": lm_status.get("model", ""),
                            "recovery_actions": ["wait", "check_lm_provider_status", "retry"],
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )
        if app.state.agent is None:
            raise HTTPException(
                status_code=503,
                detail=_agent_not_available_error(app, sid).model_dump(exclude_none=True),
            )

        if (
            req.model is not None
            and not _model_ref_is_empty(req.model)
            and not _model_ref_matches_active(req.model, app)
        ):
            active_model = _active_lm_model_ref(app)
            raise HTTPException(
                status_code=501,
                detail=_unsupported_model_ref_error(
                    session_id=sid,
                    source="per_message",
                    model_ref=req.model,
                    active_model=active_model,
                ).model_dump(exclude_none=True),
            )

        if not _model_ref_is_empty(sess.model) and not _model_ref_matches_active(sess.model, app):
            active_model = _active_lm_model_ref(app)
            if active_model.get("model_id"):
                app.state.sessions.update(sid, model={})
                sess = app.state.sessions.get(sid) or sess
            else:
                raise HTTPException(
                    status_code=501,
                    detail=_unsupported_model_ref_error(
                        session_id=sid,
                        source="session",
                        model_ref=sess.model,
                        active_model=active_model,
                    ).model_dump(exclude_none=True),
                )

        user_text = req.extract_text()
        turn_agent_id = req.extract_agent_id().strip()
        image_parts = req.image_parts()
        if image_parts and not _active_lm_supports_vision(app):
            raise HTTPException(
                status_code=501,
                detail=_image_part_error(
                    session_id=sid,
                    image_count=len(image_parts),
                    provider=_effective_lm_config(app),
                ).model_dump(exclude_none=True),
            )
        if not user_text and not image_parts:
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

        # Persist + publish the user message synchronously so by the
        # time the ack returns, GET /messages reflects it. Then mark
        # the session running, then schedule the turn in the
        # background and return.
        user_msg = _start_background_user_turn(
            sid,
            sess,
            user_text,
            request_parts=req.parts,
            metadata=req.metadata,
            prev_status="idle",
            turn_agent_id=turn_agent_id,
        )
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

    @app.get("/v1/sessions/{sid}/messages/{message_id}")
    async def get_message(sid: str, message_id: str) -> dict[str, Any]:
        """SPEC §6.3 drill-down for one stored message."""

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
        for msg in app.state.messages.get(sid, []):
            if msg.id == message_id:
                return msg.model_dump(exclude_none=True)
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"message not found: {message_id}",
                    details={"session_id": sid, "message_id": message_id},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    # ---- /v1/agents catalog (BBB10) + dynamic registry (#19) ---------

    def _agent_with_capability_refs(agent_def: AgentDef) -> AgentDef:
        """Attach normalized capability metadata to an AgentDef row."""

        refs: list[AgentCapabilityRef] = [
            AgentCapabilityRef(kind="tool", id=tool_id, title=tool_id, source="builtin")
            for tool_id in agent_def.tools
        ]
        refs.extend(
            AgentCapabilityRef(kind="skill", id=skill_id, title=skill_id, source=agent_def.source)
            for skill_id in agent_def.skills
        )
        refs.extend(
            AgentCapabilityRef(
                kind="command",
                id=command_id,
                title=command_id,
                source="builtin",
            )
            for command_id in agent_def.commands
        )
        refs.extend(agent_def.capability_refs)

        if agent_def.id == "main":
            command_ids = set(agent_def.commands)
            for row in _BACKEND_COMMANDS:
                command_id = row["id"]
                if command_id in command_ids:
                    continue
                raw_status = row.get("status")
                status: Literal["available", "unavailable", "unknown"] = (
                    raw_status
                    if raw_status in {"available", "unavailable", "unknown"}
                    else "available"
                )
                refs.append(
                    AgentCapabilityRef(
                        kind="command",
                        id=command_id,
                        title=row.get("title", command_id),
                        description=row.get("description", ""),
                        source=row.get("source", "builtin"),
                        status=status,
                        metadata=({"error": row["error"]} if row.get("error") else {}),
                    )
                )
                command_ids.add(command_id)
            agent_def = agent_def.model_copy(update={"commands": sorted(command_ids)})

        if agent_def.source == "skill" and agent_def.id not in agent_def.skills:
            refs.append(
                AgentCapabilityRef(
                    kind="skill",
                    id=agent_def.id,
                    title=agent_def.title,
                    description=agent_def.description,
                    source=str(agent_def.metadata.get("skill_source", "skill")),
                    metadata={
                        "skill_path": agent_def.metadata.get("skill_path", ""),
                        "skill_layout": agent_def.metadata.get("skill_layout", ""),
                    },
                )
            )
            agent_def = agent_def.model_copy(update={"skills": [*agent_def.skills, agent_def.id]})

        deduped: list[AgentCapabilityRef] = []
        seen: set[tuple[str, str]] = set()
        for ref in refs:
            key = (ref.kind, ref.id)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(ref)

        return agent_def.model_copy(update={"capability_refs": deduped})

    def _workspace_catalog_cwd(workspace_id: str = "", session_id: str = "") -> Path | None:
        wid = workspace_id
        if session_id:
            sess = app.state.sessions.get(session_id)
            if sess is not None:
                wid = wid or str(getattr(sess, "workspace_id", "") or "")
        if not wid:
            return None
        ws = app.state.workspaces.get(wid)
        if ws is None:
            return None
        root_path = str(getattr(ws, "root_path", "") or "")
        return Path(root_path).expanduser() if root_path else None

    def _active_session_agent_blueprint_id(session_id: str = "") -> str:
        if not session_id:
            return ""
        sess = app.state.sessions.get(session_id)
        if sess is None:
            return ""
        metadata = getattr(sess, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            return ""
        return str(metadata.get("active_agent_blueprint_id") or "").strip()

    def _active_session_agent_blueprint_path(session_id: str = "") -> Path | None:
        if not session_id:
            return None
        sess = app.state.sessions.get(session_id)
        if sess is None:
            return None
        metadata = getattr(sess, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            return None
        raw = str(metadata.get("active_agent_blueprint_path") or "").strip()
        return Path(raw).expanduser() if raw else None

    def _agent_blueprint_activation_metadata(
        *,
        blueprint_wire: Mapping[str, Any],
        install_root: Path | None,
        scope: str,
    ) -> dict[str, str]:
        install = read_install_metadata(install_root) if install_root is not None else {}
        return {
            "active_agent_blueprint_id": str(blueprint_wire.get("id") or ""),
            "active_agent_blueprint_version": str(blueprint_wire.get("version") or ""),
            "active_agent_blueprint_scope": scope,
            "active_agent_blueprint_definition_path": str(
                blueprint_wire.get("definition_path") or ""
            ),
            "active_agent_blueprint_source": str(install.get("source") or ""),
            "active_agent_blueprint_source_kind": str(install.get("source_kind") or ""),
            "active_agent_blueprint_ref": str(install.get("ref") or ""),
            "active_agent_blueprint_commit": str(install.get("commit") or ""),
            "active_agent_blueprint_checksum": str(install.get("checksum") or ""),
            "active_agent_blueprint_installed_at": str(install.get("installed_at") or ""),
        }

    def _session_agent_overlay(session_id: str = "") -> dict[str, Any]:
        if not session_id:
            return {}
        sess = app.state.sessions.get(session_id)
        if sess is None:
            return {}
        metadata = getattr(sess, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            return {}
        overlay = metadata.get("agent_blueprint_overlay")
        return dict(overlay) if isinstance(overlay, Mapping) else {}

    def _base_session_agent_blueprint_rows(
        session_id: str = "",
        workspace_id: str = "",
    ) -> list[AgentDef]:
        if not session_id:
            return []
        cwd = _workspace_catalog_cwd(workspace_id=workspace_id, session_id=session_id)
        active_blueprint_id = _active_session_agent_blueprint_id(session_id)
        active_blueprint_path = _active_session_agent_blueprint_path(session_id)
        if active_blueprint_path is not None:
            return load_agent_blueprint_path(active_blueprint_path, scope="session")
        if active_blueprint_id:
            return load_agent_blueprints(cwd=cwd, blueprint_id=active_blueprint_id)
        return []

    def _apply_agent_overlay_rows(
        rows: list[AgentDef],
        overlay: Mapping[str, Any],
        *,
        session_id: str = "",
    ) -> list[AgentDef]:
        agents = overlay.get("agents") if isinstance(overlay, Mapping) else None
        if not isinstance(agents, Mapping):
            return rows
        patchable = _agent_overlay_patchable_fields()
        out: list[AgentDef] = []
        for row in rows:
            raw_patch = agents.get(row.id)
            if not isinstance(raw_patch, Mapping):
                out.append(row)
                continue
            update = {key: value for key, value in raw_patch.items() if key in patchable}
            metadata = {
                **row.metadata,
                "agent_blueprint_overlay": {
                    "session_id": session_id,
                    "fields": sorted(update),
                    "status": "applied",
                },
            }
            out.append(row.model_copy(update={**update, "metadata": metadata}))
        return out

    def _apply_session_agent_overlay(rows: list[AgentDef], session_id: str = "") -> list[AgentDef]:
        overlay = _session_agent_overlay(session_id)
        return _apply_agent_overlay_rows(rows, overlay, session_id=session_id)

    def _enabled_agent_blueprint_mcp_tool_names(blueprint_id: str = "") -> set[str]:
        names: set[str] = set()
        for server in (getattr(app.state, "external_mcp_servers", {}) or {}).values():
            if not isinstance(server, Mapping):
                continue
            if str(server.get("status") or "") != "ready":
                continue
            if blueprint_id and str(server.get("agent_blueprint_id") or "") != blueprint_id:
                continue
            for tool in server.get("tools") or []:
                if not isinstance(tool, Mapping):
                    continue
                if not bool(tool.get("enabled")) or str(tool.get("status") or "") != "ready":
                    continue
                tool_name = str(tool.get("name") or tool.get("id") or "").strip()
                if tool_name:
                    names.add(tool_name)
        return names

    def _agent_blueprint_descriptor_tools(rows: list[AgentDef]) -> dict[str, str]:
        descriptors_by_tool: dict[str, str] = {}
        roots: dict[str, tuple[str, str]] = {}
        for row in rows:
            root_file = str(row.metadata.get("agent_blueprint_definition_path") or "").strip()
            if not root_file:
                continue
            roots[root_file] = (
                str(row.metadata.get("agent_blueprint_scope") or "session"),
                str(row.metadata.get("agent_blueprint_id") or ""),
            )
        for root_file, (scope, blueprint_id) in sorted(roots.items()):
            root = Path(root_file).expanduser().parent
            try:
                descriptors = load_mcp_descriptors(
                    root,
                    scope=scope,
                    blueprint_id=blueprint_id,
                )
            except Exception:
                continue
            for descriptor in descriptors:
                descriptor_id = str(descriptor.get("id") or "")
                for tool in descriptor.get("tools") or []:
                    if not isinstance(tool, Mapping):
                        continue
                    tool_name = str(tool.get("name") or tool.get("id") or "").strip()
                    if tool_name:
                        descriptors_by_tool[tool_name] = descriptor_id
        return descriptors_by_tool

    def _apply_agent_blueprint_mcp_descriptor_validation(rows: list[AgentDef]) -> list[AgentDef]:
        descriptor_tools = _agent_blueprint_descriptor_tools(rows)
        if not descriptor_tools:
            return rows
        out: list[AgentDef] = []
        for row in rows:
            enabled_tools = _enabled_agent_blueprint_mcp_tool_names(
                str(row.metadata.get("agent_blueprint_id") or "").strip()
            )
            errors = list(row.validation_errors)
            diagnostics = list(row.metadata.get("tool_diagnostics", []))
            for tool_name in row.tools:
                if tool_name not in descriptor_tools or tool_name in enabled_tools:
                    continue
                descriptor_id = descriptor_tools[tool_name]
                message = f"MCP tool requires explicit enablement: {tool_name}" + (
                    f" (descriptor: {descriptor_id})" if descriptor_id else ""
                )
                if message not in errors:
                    errors.append(message)
                if not any(
                    isinstance(diag, Mapping)
                    and str(diag.get("tool") or "") == tool_name
                    and str(diag.get("source") or "") == "agent_blueprint_mcp_descriptor"
                    for diag in diagnostics
                ):
                    diagnostics.append(
                        {
                            "tool": tool_name,
                            "status": "disabled",
                            "source": "agent_blueprint_mcp_descriptor",
                            "descriptor_id": descriptor_id,
                        }
                    )
            metadata = dict(row.metadata)
            if diagnostics:
                metadata["tool_diagnostics"] = diagnostics
            if errors != list(row.validation_errors):
                metadata["mcp_descriptor_validation_disabled"] = True
            out.append(
                row.model_copy(
                    update={
                        "enabled": row.enabled and not errors,
                        "validation_errors": errors,
                        "metadata": metadata,
                    }
                )
            )
        return out

    def _apply_enabled_agent_blueprint_mcp_tools(rows: list[AgentDef]) -> list[AgentDef]:
        out: list[AgentDef] = []
        cache: dict[str, set[str]] = {}
        for row in rows:
            blueprint_id = str(row.metadata.get("agent_blueprint_id") or "").strip()
            enabled_tools = cache.setdefault(
                blueprint_id,
                _enabled_agent_blueprint_mcp_tool_names(blueprint_id),
            )
            if not enabled_tools:
                out.append(row)
                continue
            row_tools = {str(tool).strip() for tool in row.tools if str(tool).strip()}
            resolved_tools = row_tools & enabled_tools
            if not resolved_tools:
                out.append(row)
                continue
            errors = [
                error
                for error in row.validation_errors
                if not any(
                    error.startswith(f"MCP tool requires explicit enablement: {tool}")
                    for tool in resolved_tools
                )
            ]
            diagnostics = [
                diag
                for diag in row.metadata.get("tool_diagnostics", [])
                if not (
                    isinstance(diag, Mapping)
                    and str(diag.get("source") or "") == "agent_blueprint_mcp_descriptor"
                    and str(diag.get("tool") or "") in resolved_tools
                )
            ]
            metadata = dict(row.metadata)
            if diagnostics:
                metadata["tool_diagnostics"] = diagnostics
            else:
                metadata.pop("tool_diagnostics", None)
            disabled_by_mcp_validation = bool(
                metadata.pop("mcp_descriptor_validation_disabled", False)
            )
            out.append(
                row.model_copy(
                    update={
                        "enabled": row.enabled or (disabled_by_mcp_validation and not errors),
                        "validation_errors": errors,
                        "metadata": metadata,
                    }
                )
            )
        return out

    def _active_session_agent_blueprint_rows(
        session_id: str = "",
        workspace_id: str = "",
    ) -> list[AgentDef]:
        if not session_id:
            return []
        rows = _base_session_agent_blueprint_rows(session_id=session_id, workspace_id=workspace_id)
        if rows:
            rows = _apply_session_agent_overlay(rows, session_id=session_id)
            prompt_registry = _prompt_registry_for_request(
                session_id=session_id,
                workspace_id=workspace_id,
            )
            rows = validate_agent_hierarchy(_merge_agent_def_rows(rows))
            rows = _apply_agent_blueprint_mcp_descriptor_validation(rows)
            rows = _apply_enabled_agent_blueprint_mcp_tools(rows)
            active_blueprint_id = _active_session_agent_blueprint_id(session_id)
            render_context = _prompt_render_context(app)
            render_context.update(_agent_rows_prompt_render_context(rows))
            render_context["session.active_agent_blueprint"] = (
                active_blueprint_id or "(no active agent blueprint)"
            )
            render_context["session.active_pack"] = active_blueprint_id or "(no active expert pack)"
            return [
                _apply_prompt_registry_to_agent(
                    app,
                    _agent_with_capability_refs(row),
                    prompt_registry=prompt_registry,
                    render_context=render_context,
                )
                for row in rows
            ]
        return []

    def _active_session_agent_blueprint_agent_ids(session_id: str = "") -> set[str]:
        return {
            row.id
            for row in _active_session_agent_blueprint_rows(session_id=session_id)
            if row.enabled
        }

    def _active_session_agent_blueprint_root_id(session_id: str = "") -> str:
        rows = _active_session_agent_blueprint_rows(session_id=session_id)
        if not rows:
            return ""
        requested_root = str(rows[0].metadata.get("agent_blueprint_root_expert") or "").strip()
        if requested_root and any(row.id == requested_root and row.enabled for row in rows):
            return requested_root
        roots = [row for row in rows if row.enabled and not row.parent_id]
        if len(roots) == 1:
            return roots[0].id
        enabled = [row for row in rows if row.enabled]
        if not enabled:
            return ""
        return sorted(enabled, key=lambda row: (row.tier, row.id))[0].id

    def _resolve_runtime_dynamic_agent(
        agent_id: str,
        *,
        session_id: str = "",
        workspace_id: str = "",
        prompt_registry: PromptRegistry | None = None,
    ) -> "AgentDef | None":
        if session_id:
            for row in _active_session_agent_blueprint_rows(
                session_id=session_id,
                workspace_id=workspace_id,
            ):
                if row.id == agent_id and row.enabled:
                    return row
        return _resolve_dynamic_agent(app, agent_id, prompt_registry=prompt_registry)

    def _agent_rows(session_id: str = "", workspace_id: str = "") -> list[AgentDef]:
        cwd = _workspace_catalog_cwd(workspace_id=workspace_id, session_id=session_id)
        rows = _active_session_agent_blueprint_rows(
            session_id=session_id,
            workspace_id=workspace_id,
        )
        if rows:
            return rows
        active_pack_id = _runtime_active_session_expert_pack_id(app, session_id)
        active_pack_path = _runtime_active_session_expert_pack_path(app, session_id)
        explicit_session_rows = (
            load_expert_pack_path(active_pack_path, scope="session")
            if active_pack_path is not None
            else []
        )
        rows = (
            _builtin_agents()
            + [AgentDef(**row.to_wire()) for row in app.state.user_agents.list()]
            + _load_skills_from_disk()
            + load_expert_packs(cwd=cwd, pack_id=active_pack_id)
            + explicit_session_rows
        )
        prompt_registry = _prompt_registry_for_request(
            session_id=session_id,
            workspace_id=workspace_id,
        )
        return [
            _apply_prompt_registry_to_agent(
                app,
                _agent_with_capability_refs(row),
                prompt_registry=prompt_registry,
            )
            for row in validate_expert_hierarchy(_merge_agent_def_rows(rows))
        ]

    # ---- /v1/agent-blueprints/* + /v1/expert-packs/* lifecycle + session
    # blueprint activation (iowarp/clio-agent#663) -----------------------
    # Blueprint source registry, install/update/delete engine, MCP-descriptor
    # enable, and the session-scoped get/set-active-blueprint routes are owned
    # by routes/blueprints.py and registered below via
    # ``register_blueprints_routes(app, deps)`` once ``deps`` is built. The
    # expert-pack routes are thin aliases of the blueprint lifecycle (one engine,
    # ``kind``-distinguished). The set-active route reaches the activation-metadata
    # builder + workspace-session mirror (and the metadata-only active-id reader)
    # through ``deps``.

    # ---- /v1/expert-packs/* discovery + session attachment ----------------
    # The expert-pack discovery (list/get/validate) and session attachment
    # (get/set active pack) routes are owned by routes/expert_packs.py and
    # registered below via ``register_expert_packs_routes(app, deps)`` once
    # ``deps`` is built. (Pack install/update/delete are blueprint-engine
    # aliases owned by routes/blueprints.py.)

    # ---- /v1/agents/* registry CRUD + extract + /v1/sessions/{sid}/agent-overlay ---
    # The Tier-2 agent registry (list/get/create/update/delete + extract) and the
    # session agent-overlay routes (get/put/export) are owned by routes/agents.py
    # and registered below via ``register_agents_routes(app, deps)`` once ``deps``
    # is built. They reach the shared row-resolution closures (``agent_rows``/
    # ``agent_with_capability_refs``/``base_session_agent_blueprint_rows``/
    # ``apply_agent_overlay_rows``/``prompt_registry_for_request``) plus the
    # destructive-action guard and workspace-session mirror through ``deps``.

    # Cross-concern seam (#714): built once and threaded to every extracted
    # ``register_<concern>_routes(app, deps)`` factory so moved handlers reach
    # shared ``build_app``-local helpers via ``deps`` rather than closing over
    # them. Built here, after every closure it carries is defined. Keep minimal
    # — add a field only when a moved handler needs it.
    deps = GactDeps(
        guard_direct_destructive_action=_guard_direct_destructive_action,
        apply_edit_to_disk=_apply_edit_to_disk,
        flush_context_files=_flush_context_files,
        prompt_registry_for_request=_prompt_registry_for_request,
        prompt_agent_overlay_for_request=_prompt_agent_overlay_for_request,
        prompt_render_context_for_request=_prompt_render_context_for_request,
        active_session_agent_blueprint_id=_active_session_agent_blueprint_id,
        agent_blueprint_activation_metadata=_agent_blueprint_activation_metadata,
        mirror_workspace_session=_mirror_workspace_session,
        agent_rows=_agent_rows,
        agent_with_capability_refs=_agent_with_capability_refs,
        base_session_agent_blueprint_rows=_base_session_agent_blueprint_rows,
        apply_agent_overlay_rows=_apply_agent_overlay_rows,
        append_session_message=_append_session_message,
        delete_session_messages=_delete_session_messages,
        blueprint_runner_for_agent=_blueprint_runner_for_agent,
        resolve_runtime_dynamic_agent=_resolve_runtime_dynamic_agent,
    )

    # ---- /v1/workspaces (CLIO-BBBBBBBBBB-WS) -------------------------
    # Workspace store CRUD + file listing/reading are owned by
    # routes/workspaces.py; registered here so they bind to the same app.
    register_workspaces_routes(app, deps)

    # ---- /v1/agent-blueprints/* + /v1/expert-packs/* + session blueprint ---
    # Blueprint source registry, install/update/delete engine, MCP-descriptor
    # enable, and the session get/set-active-blueprint routes are owned by
    # routes/blueprints.py; the expert-pack routes are thin aliases of the same
    # lifecycle. The set-active route reaches the activation-metadata builder,
    # workspace-session mirror, and metadata-only active-id reader through
    # ``deps``.
    register_blueprints_routes(app, deps)

    # ---- /v1/expert-packs/* discovery + session attachment -----------
    # Pack discovery (list/get/validate) and session attachment (get/set the
    # active pack) are owned by routes/expert_packs.py; the set route reaches
    # the workspace-session mirror through ``deps``. (Pack install/update/delete
    # are blueprint-engine aliases registered above by register_blueprints_routes.)
    register_expert_packs_routes(app, deps)

    # ---- /v1/agents/* + /v1/sessions/{sid}/agent-overlay -------------
    # Tier-2 agent registry CRUD + list + extract and the session agent-overlay
    # routes (get/put/export) are owned by routes/agents.py; they reach the shared
    # row-resolution closures plus the destructive-action guard and workspace-
    # session mirror through ``deps``.
    register_agents_routes(app, deps)

    # ---- /v1/mcp/servers (#13) ---------------------------------------
    # MCP server registry + dispatch (list/detail/install/call/reconnect/
    # uninstall + tools/resources/prompts + handshake) are owned by
    # routes/mcp.py; the uninstall route reaches the destructive-action guard
    # through ``deps``.
    register_mcp_routes(app, deps)

    # ---- /v1/prompts (CLIO prompt-management vendor surface) ---------
    # Prompt registry browse/render/validate/save/reload are owned by
    # routes/prompts.py; the request-scoped registry/overlay/render-context
    # builders travel on ``deps``.
    register_prompts_routes(app, deps)

    # ---- /v1/sessions/{sid}/context/* (ARC live-context plane) -------
    # Session context compartment policy + the live ARC context-plane routes
    # (state/ops/compact/search) are owned by routes/context.py; the
    # state-assembly + ARC-unavailable helpers they share live there.
    register_context_routes(app, deps)

    # ---- /v1/sessions/{sid}/diffs/* + /context/files + /context/frames ---
    # Pending/applied file-diff list/apply/reject plus the context-file
    # attach/detach/list ledger and per-turn context frames are owned by
    # routes/diffs.py; the diff-to-disk commit + ledger flush + destructive-
    # action guard travel on ``deps``.
    register_diffs_routes(app, deps)

    # ---- /v1/memory/* (transcript-memory recall surface) -------------
    # The read-only memory search + the three agent-callable, policy-gated
    # memory tools (search-sessions / read-session-summary / read-context-frame)
    # are owned by routes/memory.py. The ranked-search primitives they share with
    # the agent-run path live in runtime/memory_search.py (single source); the
    # error/audit/policy + bounded-projection helpers are private to that module.
    register_memory_routes(app, deps)

    # ---- /v1/sessions/{sid}/schedules + /v1/schedules/{id} (#21) -----
    # Scheduled-turn CRUD (list/add/delete) is owned by routes/schedules.py; the
    # delete route reaches the destructive-action guard through ``deps`` and the
    # scheduler tick task owns the actual firing of due schedules.
    register_schedules_routes(app, deps)

    # ---- /v1/health + /v1/capabilities + /v1/capability-gaps + /v1/metrics ----
    # + /v1/memory/stats: the read-only system/observability surface is owned by
    # routes/system.py. The static capability/metrics catalogs it projects live in
    # runtime/capabilities.py (shared with the message-turn streaming path here);
    # the wire/limit constants live in runtime/constants.py. It needs no
    # cross-concern seam from ``deps``.
    register_system_routes(app, deps)

    # ---- /v1/sessions/{sid}/tasks + /v1/tasks/{tid} + memory/events + share ----
    # + /v1/shared/{token} + /v1/sessions/{sid}/events SSE: the misc session-
    # adjacent surfaces are owned by routes/misc.py; the task-delete route reaches
    # the direct-destructive-action guard through ``deps``.
    register_misc_routes(app, deps)

    # ---- /v1/catalog/tools + /v1/tools + /v1/commands + dispatch -----
    # The tool catalog (built-in + unified live) and the slash-command catalog +
    # dispatch are owned by routes/catalog.py. The command-table assembly lives in
    # runtime/commands.py (shared with the prompt-render-context closure here); the
    # dispatch route reaches the message-ledger primitives, agent runner, and
    # destructive-action guard through ``deps``.
    register_catalog_routes(app, deps)

    # ---- /v1/providers/lm (CLIO-BBBBBBBBBB-D) ------------------------

    # Derived from clio_agent.providers.registry. Add new presets to
    # the registry, not here — this list reflects whatever the registry
    # contains at build_app() time. Polaris preset removed for the time
    # being — the inference-api gateway returns 400 'cluster polaris
    # does not exist' for /resource_server/polaris/vllm/v1.
    from clio_agent.providers.registry import as_lm_presets as _build_lm_presets

    _LM_PRESETS: list[LMProviderPreset] = _build_lm_presets()

    def _normalize_lm_provider_request(req: LMProviderRequest) -> LMProviderRequest:
        """Convert catalog preset ids to runtime provider kinds before wiring DSPy."""

        preset = next((p for p in _LM_PRESETS if p.id == req.provider), None)
        if preset is None:
            return req
        provider_kind = _provider_runtime_kind(req.provider)
        if provider_kind == req.provider:
            return req
        return req.model_copy(
            update={
                "provider": provider_kind,
                "api_base": req.api_base or preset.api_base,
                "model": req.model or preset.suggested_model,
            }
        )

    def _preset_api_key_env(preset: LMProviderPreset) -> str:
        if preset.api_key_env:
            return preset.api_key_env
        return {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
        }.get(preset.id, "CLIO_LM_API_KEY")

    def _which_cli(*names: str) -> str | None:
        """Resolve a local CLI across POSIX names and Windows shims."""

        for name in names:
            found = shutil.which(name)
            if found:
                return found
            if os.name == "nt" and not name.lower().endswith((".cmd", ".exe")):
                for suffix in (".cmd", ".exe"):
                    found = shutil.which(name + suffix)
                    if found:
                        return found
        return None

    def _preset_with_status(preset: LMProviderPreset) -> LMProviderPreset:
        update: dict[str, Any] = {}
        if preset.provider == "argonne":
            env_token = (
                os.environ.get("CLIO_ARGONNE_TOKEN", "").strip()
                or os.environ.get("ALCF_INFERENCE_TOKEN", "").strip()
                or os.environ.get("access_token", "").strip()
            )
            if env_token:
                update["status"] = "ready"
                update["status_message"] = "ALCF token present in environment"
                update["is_authenticated"] = True
                return preset.model_copy(update=update)
            try:
                from clio_agent.providers import argonne_auth  # noqa: PLC0415
            except Exception as exc:
                update["status"] = "unavailable"
                update["status_message"] = f"argonne auth unavailable: {exc}"
                update["is_authenticated"] = False
                return preset.model_copy(update=update)
            if not argonne_auth.tokens_exist():
                update["status"] = "auth_required"
                update["status_message"] = (
                    "no Globus token stored; authenticate ALCF before connecting"
                )
                update["is_authenticated"] = False
                return preset.model_copy(update=update)
            if argonne_auth.check_auth_status():
                update["status"] = "ready"
                update["status_message"] = "Globus token validated"
                update["is_authenticated"] = True
                return preset.model_copy(update=update)
            update["status"] = "auth_required"
            update["status_message"] = (
                "stored Globus token could not be refreshed; authenticate ALCF"
            )
            update["is_authenticated"] = False
            return preset.model_copy(update=update)
        if preset.requires_api_key:
            env_key = _preset_api_key_env(preset)
            if not (os.environ.get(env_key) or os.environ.get("CLIO_LM_API_KEY")):
                update["status"] = "missing_key"
                update["status_message"] = f"missing {env_key}"
                update["is_authenticated"] = False
                return preset.model_copy(update=update)
            update["is_authenticated"] = True
        if preset.provider == "codex":
            if _which_cli("codex"):
                update["status"] = "ready"
                update["status_message"] = "codex CLI available"
                update["is_authenticated"] = True
            else:
                update["status"] = "unavailable"
                update["status_message"] = "codex CLI not found on PATH"
                update["is_authenticated"] = False
            return preset.model_copy(update=update)
        if preset.provider == "claude_code":
            if _which_cli("claude"):
                update["status"] = "ready"
                update["status_message"] = "claude CLI available"
                update["is_authenticated"] = True
            else:
                update["status"] = "unavailable"
                update["status_message"] = "claude CLI not found on PATH"
                update["is_authenticated"] = False
            return preset.model_copy(update=update)
        if not preset.supports_live_catalog:
            update["status"] = "ready"
            update["status_message"] = "static catalog"
            update["is_authenticated"] = True
            return preset.model_copy(update=update)
        update["status"] = "unknown"
        update["status_message"] = ""
        update.setdefault("is_authenticated", not preset.requires_api_key)
        return preset.model_copy(update=update)

    def _lm_presets_with_status() -> list[LMProviderPreset]:
        return sorted(
            (_preset_with_status(preset) for preset in _LM_PRESETS),
            key=lambda p: p.label.lower(),
        )

    def _lm_provider_status() -> dict[str, Any]:
        status = getattr(app.state, "lm_config_status", None)
        if not isinstance(status, dict):
            return {"state": "idle"}
        return status

    def _lm_provider_info(*, presets: list[LMProviderPreset] | None = None) -> LMProviderInfo:
        cfg = _effective_lm_config(app)
        status = _lm_provider_status()
        state = str(status.get("state") or "idle")
        if state not in {"idle", "configuring", "ready", "error"}:
            state = "idle"
        pending = status if state == "configuring" else {}
        return LMProviderInfo(
            configured=app.state.agent is not None and state != "configuring",
            provider=str(pending.get("provider") or cfg.get("provider", "")),
            api_base=str(pending.get("api_base") or cfg.get("api_base", "")),
            model=str(pending.get("model") or cfg.get("model", "")),
            temperature=(
                float(pending["temperature"])
                if pending.get("temperature") is not None
                else float(cfg["temperature"])
                if cfg.get("temperature") is not None
                # Mirror LMProviderConfig's deterministic default (0.0) when no
                # provider is configured yet, instead of re-surfacing the old
                # 1.0 sampler default that the agentic structured-output path
                # never wants. Keeps the idle /v1/providers/lm echo consistent
                # with what an omitted-temperature PUT actually binds.
                else 0.0
            ),
            max_tokens=(
                int(pending["max_tokens"])
                if pending.get("max_tokens") is not None
                else int(cfg["max_tokens"])
                if cfg.get("max_tokens") is not None
                else 32000
            ),
            context_length=(
                int(pending["context_length"])
                if pending.get("context_length") is not None
                else int(cfg["context_length"])
                if cfg.get("context_length") is not None
                else 0
            ),
            chosen_context=(
                int(cfg["chosen_context"]) if cfg.get("chosen_context") is not None else None
            ),
            context_window=(
                int(cfg["context_window"]) if cfg.get("context_window") is not None else None
            ),
            is_reasoning=bool(cfg.get("is_reasoning") or False),
            native_tool_calling=bool(cfg.get("native_tool_calling") or False),
            thinking_budget=(
                int(pending["thinking_budget"])
                if pending.get("thinking_budget") is not None
                else int(cfg["thinking_budget"])
                if cfg.get("thinking_budget") is not None
                else 0
            ),
            transport=pending.get("transport") or cfg.get("transport"),
            state=state,  # type: ignore[arg-type]
            status_message=str(status.get("message") or ""),
            error=str(status.get("error") or ""),
            operation_id=str(status.get("operation_id") or ""),
            presets=presets if presets is not None else _lm_presets_with_status(),
        )

    @app.get("/v1/providers/lm", response_model=LMProviderInfo)
    async def get_lm_provider() -> LMProviderInfo:
        """Report the live LM config — what we'd report on /doctor as
        the 'lm' integration row, plus a list of presets the TUI's
        provider picker shows.

        ``configured`` is true when an agent is wired and ready to
        run; the TUI uses this to decide whether to show the config
        modal on connect.
        """

        return _lm_provider_info()

    async def _apply_lm_provider(req: LMProviderRequest) -> LMProviderInfo:
        """Reconfigure the LM in-place. Rebuilds DSPy + the
        ClioAgent so subsequent POST /messages drive the new
        provider. The old agent's state (ARC, sessions, in-flight
        messages) is preserved across the swap.
        """

        req = _normalize_lm_provider_request(req)
        env_keys = (
            "CLIO_LM_PROVIDER",
            "CLIO_LM_API_BASE",
            "CLIO_LM_MODEL",
            "CLIO_LM_API_KEY",
            "CLIO_CODEX_TRANSPORT",
        )
        env_before = {key: os.environ.get(key) for key in env_keys}
        dspy_settings_before: dict[str, Any] | None = None
        settings_sentinel = object()

        def _restore_process_env() -> None:
            for key, value in env_before.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        def _restore_dspy_settings() -> None:
            if dspy_settings_before is None:
                return
            try:
                from dspy.dsp.utils.settings import main_thread_config  # noqa: PLC0415
            except Exception:
                return
            for key, value in dspy_settings_before.items():
                if value is settings_sentinel:
                    main_thread_config.pop(key, None)
                else:
                    main_thread_config[key] = value

        def _stamp_process_env(cfg: "LMProviderConfig", api_key: str) -> None:
            os.environ["CLIO_LM_PROVIDER"] = req.provider
            os.environ["CLIO_LM_API_BASE"] = req.api_base
            os.environ["CLIO_LM_MODEL"] = req.model
            os.environ["CLIO_LM_API_KEY"] = api_key
            if req.provider == "codex":
                os.environ["CLIO_CODEX_TRANSPORT"] = cfg.codex_transport
            else:
                os.environ.pop("CLIO_CODEX_TRANSPORT", None)

        def _apply_lm_studio_load_config() -> None:
            """Apply LM Studio load-time options before wiring DSPy."""

            if req.provider != "lm_studio" or req.context_length <= 0:
                return

            import requests  # noqa: PLC0415

            root = _lm_studio_api_root(req.api_base)
            if not root:
                raise RuntimeError("LM Studio api_base is empty")

            headers = _lm_studio_headers()
            # Backend concurrency cap (LM Studio "Max Concurrent Predictions").
            # Default 1: the agent fans out parallel sub-calls and a single-GPU
            # box wedges when the backend serves them concurrently, so serialize.
            _lm_studio_parallel = int(req.parallel) if req.parallel and req.parallel > 0 else 1

            def _already_loaded_with_requested_context() -> str:
                try:
                    response = requests.get(
                        f"{root}/api/v1/models",
                        headers=headers,
                        timeout=10,
                    )
                    if response.status_code >= 400:
                        return ""
                    payload = response.json()
                except Exception:
                    return ""

                models = payload.get("models")
                if not isinstance(models, list):
                    return ""
                for item in models:
                    if not isinstance(item, dict):
                        continue
                    key = str(item.get("key") or "")
                    loaded = item.get("loaded_instances")
                    if not isinstance(loaded, list):
                        continue
                    for instance in loaded:
                        if not isinstance(instance, dict):
                            continue
                        instance_id = str(instance.get("id") or "")
                        if req.model not in {key, instance_id}:
                            continue
                        config = instance.get("config")
                        if not isinstance(config, dict):
                            continue
                        try:
                            loaded_context = int(config.get("context_length") or 0)
                        except (TypeError, ValueError):
                            loaded_context = 0
                        try:
                            loaded_parallel = int(config.get("parallel") or 0)
                        except (TypeError, ValueError):
                            loaded_parallel = 0
                        # Reuse only if BOTH the context and the concurrency cap
                        # already match what we'd load — otherwise a stale
                        # parallel=4 instance would be kept and keep stalling.
                        if loaded_context == req.context_length and (
                            loaded_parallel == _lm_studio_parallel
                        ):
                            return instance_id
                return ""

            loaded_instance_id = _already_loaded_with_requested_context()
            if loaded_instance_id:
                _release_owned_lm_studio_instance(
                    app,
                    skip_instance_id=loaded_instance_id,
                    raise_on_error=True,
                )
                return

            _release_owned_lm_studio_instance(app, raise_on_error=True)
            response = requests.post(
                f"{root}/api/v1/models/load",
                headers=headers,
                json={
                    "model": req.model,
                    "context_length": req.context_length,
                    # LM Studio's "Max Concurrent Predictions". The agent issues
                    # parallel sub-calls; a single-GPU backend stalls/OOMs when it
                    # serves them concurrently, so cap it (default 1) and let
                    # concurrent pipeline calls queue. Overridable via req.parallel.
                    "parallel": _lm_studio_parallel,
                    # Flash attention drastically cuts KV-cache memory. Without it,
                    # a 9B model at a large context (e.g. 65536) on a 16GB card
                    # fills VRAM as a multi-stage agent run accumulates context and
                    # LM Studio WEDGES mid-run (the model stops responding even to a
                    # 1-token probe -> the no-progress watchdog kills the run).
                    # Enabling it is what makes the shareable local driver survive a
                    # full pipeline. Opt out with CLIO_LMSTUDIO_FLASH_ATTENTION=0.
                    "flash_attention": os.environ.get("CLIO_LMSTUDIO_FLASH_ATTENTION", "")
                    .strip()
                    .lower()
                    not in {"0", "false", "no", "off"},
                    "echo_load_config": True,
                },
                timeout=180,
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    "LM Studio model load failed "
                    f"({response.status_code}): {(response.text or '')[:300]}"
                )
            try:
                payload = response.json()
            except Exception:
                payload = {}
            instance_id = str(payload.get("instance_id") or "").strip()
            if instance_id:
                app.state.lm_studio_owned_instance = {
                    "root": root,
                    "instance_id": instance_id,
                    "model": req.model,
                    "context_length": req.context_length,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }

        try:
            import dspy

            from clio_agent.agent import ClioAgent
            from clio_agent.config import (
                LMProviderConfig,
                create_lm,
            )

            try:
                from dspy.dsp.utils.settings import main_thread_config  # noqa: PLC0415

                dspy_settings_before = {
                    "lm": main_thread_config.get("lm", settings_sentinel),
                    "adapter": main_thread_config.get("adapter", settings_sentinel),
                }
            except Exception:
                dspy_settings_before = None

            # Argonne / ALCF: if the TUI didn't ship an api_key, mint
            # one from the user's stored Globus session. ``LMProviderConfig``
            # will do this lazily inside __post_init__ too, but we resolve
            # eagerly here so the env mirror below carries the real token
            # for ClioAgent's reconstruction (load_config_from_env reads
            # CLIO_LM_API_KEY first, before LMProviderConfig defaults run).
            resolved_api_key = req.api_key
            if req.provider == "argonne" and _is_placeholder_api_key(resolved_api_key):
                auth_exc: Exception | None
                try:
                    resolved_api_key = _resolve_argonne_runtime_api_key()
                except Exception as exc:
                    resolved_api_key = ""
                    auth_exc = exc
                else:
                    auth_exc = None
                if not resolved_api_key:
                    raise HTTPException(
                        status_code=401,
                        detail=ErrorEnvelope(
                            error=ErrorInfo(
                                error="argonne_auth_required",
                                message=(
                                    "ALCF provider selected but no Globus token "
                                    "is available. Run "
                                    "`python -m clio_agent.providers.argonne_auth "
                                    "authenticate` once, or pass api_key in this "
                                    "request."
                                ),
                                recoverable=True,
                            )
                        ).model_dump(exclude_none=True),
                    ) from auth_exc

            cfg = LMProviderConfig(
                provider=req.provider,  # type: ignore[arg-type]  # str validated at boundary
                api_base=req.api_base,
                model=req.model,
                api_key=resolved_api_key or "x",
                temperature=req.temperature,
                max_tokens=req.max_tokens,
                top_p=req.top_p,
                top_k=req.top_k,
                min_p=req.min_p,
                presence_penalty=req.presence_penalty,
                thinking_budget=req.thinking_budget,
                codex_transport=req.transport or "exec",
            )
            # Per-provider handshake: discover connectivity + per-model config and
            # fold it into cfg — context-aware max_tokens (replacing the static ALCF
            # 4096 cap on 128-256K-context models), reasoning/tool capability flags,
            # and the queryable chosen_context. Never block a bind on a handshake
            # failure: fall back to the static config unchanged.
            handshake_report = None
            try:
                from clio_agent.providers.handshake import (  # noqa: PLC0415
                    HandshakeContext,
                    run_handshake,
                )

                handshake_report = await run_handshake(
                    HandshakeContext(
                        provider_id=req.provider,
                        provider_kind=req.provider,
                        api_base=req.api_base,
                        api_key=resolved_api_key or "",
                        target_model=req.model,
                        auth_mode="active",
                    ),
                    force=True,
                )
                cfg.apply_handshake(handshake_report, user_set_max_tokens=(req.max_tokens or 0) > 0)
            except Exception:
                handshake_report = None
            app.state.lm_handshake_report = handshake_report
            await asyncio.get_running_loop().run_in_executor(
                None,
                _apply_lm_studio_load_config,
            )
            # iowarp/clio-agent — DSPy 3.x forbids dspy.configure()
            # being re-called from a different async task than the
            # first one. PUT /v1/providers/lm comes from the FastAPI
            # request task, never the boot task, so the second call
            # always blew up. Side-step the guard by mutating
            # ``settings.main_thread_config['lm']`` directly — same
            # underlying state DSPy's __getattr__ reads, no async
            # task ownership check.
            new_lm = create_lm(cfg)
            from clio_agent.config import (  # noqa: PLC0415
                create_chat_adapter,
                create_planner_lm,
            )

            new_adapter = create_chat_adapter(cfg)
            new_planner_lm = create_planner_lm(cfg)
            try:
                from dspy.dsp.utils.settings import main_thread_config  # noqa: PLC0415

                main_thread_config["lm"] = new_lm
                main_thread_config["adapter"] = new_adapter
            except Exception:  # pragma: no cover - dspy missing
                dspy.configure(lm=new_lm, adapter=new_adapter)
            # Hot-swap the LM on the existing agent instead of
            # rebuilding from scratch. ClioAgent's expensive state
            # (ARC retriever, LSM tree, registry, expert instances,
            # tool gateways) is LM-independent — rebuilding it for
            # every Save+Connect costs ~5-10 s and is exactly the
            # latency the user complained about. These attribute
            # swaps cover the LM-dependent surface:
            #   * _provider_config   -> health/config surfaces the new provider
            #   * _main_lm           -> chat + answer synthesis use the new lm
            #   * _planner_lm        -> planner runs with the new lm
            #   * _dspy_adapter      -> local backends keep text ChatAdapter mode
            #   * dspy.settings.lm   -> experts pick it up via dspy.context()
            # Only rebuild from scratch when no agent yet exists
            # (first-connect lifecycle: the deferred-construction
            # task hasn't completed).
            existing = app.state.agent
            if existing is not None:
                existing._provider_config = cfg
                existing._main_lm = new_lm
                existing._planner_lm = new_planner_lm
                existing._router_lm = new_planner_lm
                existing._dspy_adapter = new_adapter
                agent = existing
            else:
                # First-time agent construction still reads the provider from
                # env; restore the snapshot if construction rejects it. Inject the
                # ONE per-process ARC so this build reuses it (no per-bind ARC churn).
                _stamp_process_env(cfg, resolved_api_key or "x")
                bound_arc = _process_arc(app)
                agent = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: ClioAgent(verbose=False, arc=bound_arc)
                )
                # The fresh agent built its config + LMs from env (pre-handshake);
                # carry the handshake-applied cfg + cfg-based LMs onto it so the
                # context-aware max_tokens / chosen_context are in effect on the
                # very first bind, not just on subsequent hot-swaps.
                agent._provider_config = cfg
                agent._main_lm = new_lm
                agent._planner_lm = new_planner_lm
                agent._router_lm = new_planner_lm
                agent._dspy_adapter = new_adapter
        except HTTPException:
            # Argonne auth path raises a structured 401 above; keep its
            # error code intact instead of flattening to a generic 400.
            _restore_process_env()
            _restore_dspy_settings()
            raise
        except Exception as exc:  # noqa: BLE001
            _restore_process_env()
            _restore_dspy_settings()
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
        _stamp_process_env(cfg, resolved_api_key or "x")
        app.state.agent = agent
        # The bind swaps in a freshly-built agent (new ARCMemory); _set_app_arc
        # re-wires the arc.op op-logger (every real run binds — without it the live
        # path stays unobserved).
        _set_app_arc(app, agent.arc)
        _install_tool_runtime_hooks(app)
        app.state.lm_config = {
            "provider": req.provider,
            "api_base": req.api_base,
            "model": req.model,
            "temperature": req.temperature,
            "max_tokens": req.max_tokens,
            "context_length": req.context_length,
            "thinking_budget": req.thinking_budget,
            "turn_timeout_s": req.turn_timeout_s,
            "transport": cfg.codex_transport if req.provider == "codex" else None,
        }
        _clear_session_model_refs(app)
        # Publish so live SSE subscribers see the swap (TUI updates
        # its model chip without polling).
        app.state.bus.publish(
            Event(
                type="lm.provider.changed",
                session_id="",
                payload={
                    "provider": req.provider,
                    "model": req.model,
                    "api_base": req.api_base,
                    "temperature": req.temperature,
                    "max_tokens": req.max_tokens,
                    "context_length": req.context_length,
                    "transport": cfg.codex_transport if req.provider == "codex" else None,
                },
            )
        )
        return LMProviderInfo(
            configured=True,
            provider=req.provider,
            api_base=req.api_base,
            model=req.model,
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            context_length=req.context_length,
            thinking_budget=req.thinking_budget,
            transport=cfg.codex_transport if req.provider == "codex" else None,
            presets=_lm_presets_with_status(),
        )

    async def _run_lm_provider_apply(req: LMProviderRequest, operation_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(
                None,
                lambda: asyncio.run(_apply_lm_provider(req)),
            )
        except HTTPException as exc:
            detail = exc.detail
            if isinstance(detail, dict):
                err = detail.get("error")
                if isinstance(err, dict):
                    error_code = str(err.get("error") or "config_error")
                    message = str(err.get("message") or exc)
                else:
                    error_code = "config_error"
                    message = str(detail)
            else:
                error_code = "config_error"
                message = str(detail or exc)
            app.state.lm_config_status = {
                "state": "error",
                "operation_id": operation_id,
                "provider": req.provider,
                "api_base": req.api_base,
                "model": req.model,
                "error": error_code,
                "message": message,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            app.state.bus.publish(
                Event(
                    type="lm.provider.failed",
                    session_id="",
                    payload={
                        "operation_id": operation_id,
                        "provider": req.provider,
                        "model": req.model,
                        "error": error_code,
                        "message": message,
                    },
                )
            )
        except Exception as exc:  # noqa: BLE001
            app.state.lm_config_status = {
                "state": "error",
                "operation_id": operation_id,
                "provider": req.provider,
                "api_base": req.api_base,
                "model": req.model,
                "error": "config_error",
                "message": f"failed to configure LM: {exc}",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            app.state.bus.publish(
                Event(
                    type="lm.provider.failed",
                    session_id="",
                    payload={
                        "operation_id": operation_id,
                        "provider": req.provider,
                        "model": req.model,
                        "error": "config_error",
                        "message": f"failed to configure LM: {exc}",
                    },
                )
            )
        else:
            app.state.lm_config_status = {
                "state": "ready",
                "operation_id": operation_id,
                "provider": info.provider,
                "api_base": info.api_base,
                "model": info.model,
                "temperature": info.temperature,
                "max_tokens": info.max_tokens,
                "context_length": info.context_length,
                "thinking_budget": info.thinking_budget,
                "transport": info.transport,
                "message": "LM provider ready",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

    @app.put("/v1/providers/lm", response_model=LMProviderInfo)
    async def put_lm_provider(req: LMProviderRequest) -> LMProviderInfo:
        """Start or perform an LM provider swap without freezing the backend."""

        req = _normalize_lm_provider_request(req)
        running_task = getattr(app.state, "lm_config_task", None)
        if running_task is not None and not running_task.done():
            status = _lm_provider_status()
            raise HTTPException(
                status_code=409,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="provider_configuring",
                        message="LM provider configuration is already in progress.",
                        details={
                            "operation_id": status.get("operation_id", ""),
                            "provider": status.get("provider", ""),
                            "model": status.get("model", ""),
                            "recovery_actions": ["wait", "check_lm_provider_status"],
                        },
                        recoverable=True,
                    )
                ).model_dump(exclude_none=True),
            )

        # LM Studio model loads/context changes and ALCF Globus token
        # refresh/provider wiring can block long enough to make the
        # selector feel frozen. Run those swaps in the background so
        # capability, health, agent catalog, and provider-selector
        # requests stay responsive.
        if req.provider in {"lm_studio", "argonne"}:
            operation_id = f"lmcfg_{uuid.uuid4().hex[:12]}"
            provider_label = "LM Studio" if req.provider == "lm_studio" else "ALCF"
            app.state.lm_config_status = {
                "state": "configuring",
                "operation_id": operation_id,
                "provider": req.provider,
                "api_base": req.api_base,
                "model": req.model,
                "temperature": req.temperature,
                "max_tokens": req.max_tokens,
                "context_length": req.context_length,
                "thinking_budget": req.thinking_budget,
                "transport": req.transport,
                "message": f"{provider_label} provider configuration is in progress.",
                "started_at": datetime.now(timezone.utc).isoformat(),
            }
            task = asyncio.create_task(_run_lm_provider_apply(req, operation_id))
            app.state.lm_config_task = task
            return _lm_provider_info()

        info = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: asyncio.run(_apply_lm_provider(req)),
        )
        app.state.lm_config_status = {
            "state": "ready",
            "operation_id": "",
            "provider": info.provider,
            "api_base": info.api_base,
            "model": info.model,
            "temperature": info.temperature,
            "max_tokens": info.max_tokens,
            "context_length": info.context_length,
            "thinking_budget": info.thinking_budget,
            "transport": info.transport,
            "message": "LM provider ready",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        return _lm_provider_info(presets=info.presets)

    @app.get("/v1/providers/lm/wait", response_model=LMProviderInfo)
    async def wait_lm_provider(timeout: float = 60.0) -> LMProviderInfo:
        """Block until the LM provider reaches a terminal state, then return it.

        The bind (``PUT /v1/providers/lm``) is async — it returns immediately and
        wires the LM through an ``idle -> configuring -> ready`` (or ``error``) state
        machine in the background. This endpoint lets any caller *await* readiness in
        a single request instead of re-implementing a client-side poll loop: it
        blocks server-side while the provider is ``configuring`` and returns the
        ``LMProviderInfo`` the moment it is ``ready``/``error`` (or ``idle`` — nothing
        pending), or when ``timeout`` (capped at 600s) elapses. Idempotent.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, min(float(timeout), 600.0))
        while True:
            status = getattr(app.state, "lm_config_status", {}) or {"state": "idle"}
            if str(status.get("state") or "idle") in ("ready", "error", "idle"):
                break
            if loop.time() >= deadline:
                break
            await asyncio.sleep(0.2)
        return _lm_provider_info()

    @app.get("/v1/providers/{provider_id}")
    async def get_provider(provider_id: str) -> dict[str, Any]:
        """SPEC §6.12 detail endpoint for one provider preset."""

        preset = next((p for p in _LM_PRESETS if p.id == provider_id), None)
        if preset is None:
            raise HTTPException(
                status_code=404,
                detail=ErrorEnvelope(
                    error=ErrorInfo(
                        error="not_found",
                        message=f"unknown provider: {provider_id}",
                        details={"available": [p.id for p in _LM_PRESETS]},
                        recoverable=False,
                    )
                ).model_dump(exclude_none=True),
            )
        return _provider_to_wire(preset)

    # ---- 501 stubs for the still-unwired v0.2 surface ----------------

    _stub_routes: list[tuple[str, str, str]] = [
        # (method, path, capability_name_for_error)
        # /v1/tools moved out of stubs — implemented below.
    ]

    # ---- /v1/catalog/tools + /v1/tools + /v1/tools/{tool_id} ----------
    # The built-in tool catalog and the unified live catalog (bundled gateway +
    # installed third-party MCP servers) are owned by routes/catalog.py and
    # registered below via register_catalog_routes(app, deps).

    # ---- /v1/hooks (SPEC §6.17 declarative hooks) --------------------
    # Declarative event-hook CRUD is owned by routes/hooks.py; the
    # direct-destructive-action guard the delete route needs travels on
    # ``deps``. Distinct from clio_agent.runtime.hooks (in-process Python
    # hooks the framework fires on tool/message events).
    register_hooks_routes(app, deps)

    # ---- /v1/permissions (BBB23) + /v1/policies (SPEC §6.11.b) --------
    # Permission-request ledger CRUD (list/resolve) + declarative permission-
    # policy CRUD (list/replace) are owned by routes/permissions.py; the
    # resolution-derived-policy + validation/persistence data layer lives in
    # runtime/permission_policies.py (shared with the build_app startup load).
    register_permissions_routes(app, deps)

    # ---- DELETE /v1/messages/{id} ------------------------------------
    #
    # gact-tui's "delete this message" gesture (used in the search
    # palette + the per-message context menu) historically hit the
    # global route. Prefer the session-scoped route so destructive
    # message deletion cannot accidentally cross session boundaries.
    # Publishes message.deleted so SSE subscribers can redraw without
    # polling.

    def _delete_message_from_session(sid: str, message_id: str) -> bool:
        msgs = app.state.messages.get(sid, [])
        for i, message in enumerate(msgs):
            if message.id != message_id:
                continue
            sess = app.state.sessions.get(sid)
            _guard_direct_destructive_action(
                app,
                session_id=sid,
                workspace_id=getattr(sess, "workspace_id", ""),
                tool_name="gact.message.delete",
                args={"message_id": message_id, "session_id": sid},
                summary=f"delete message {message_id} from session {sid}",
                reason="user_requested_message_delete",
            )
            msgs.pop(i)
            _replace_session_messages(app, sid, msgs)
            if sess is not None:
                app.state.sessions.update(sid, message_count=len(msgs))
            app.state.bus.publish(
                Event(
                    type="message.deleted",
                    session_id=sid,
                    payload={"message_id": message_id, "session_id": sid},
                )
            )
            return True
        return False

    def _message_not_found(message_id: str, *, session_id: str = "") -> HTTPException:
        details = {"message_id": message_id}
        if session_id:
            details["session_id"] = session_id
        return HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"message not found: {message_id}",
                    details=details,
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

    @app.delete("/v1/sessions/{sid}/messages/{message_id}")
    async def delete_session_message(sid: str, message_id: str) -> Response:
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
        if _delete_message_from_session(sid, message_id):
            return Response(status_code=204)
        raise _message_not_found(message_id, session_id=sid)

    @app.delete("/v1/messages/{message_id}")
    async def delete_message(message_id: str, session_id: str = "") -> Response:
        if session_id:
            if app.state.sessions.get(session_id) is None:
                raise HTTPException(
                    status_code=404,
                    detail=ErrorEnvelope(
                        error=ErrorInfo(
                            error="internal_error",
                            message=f"session not found: {session_id}",
                            details={"session_id": session_id},
                            recoverable=False,
                        )
                    ).model_dump(exclude_none=True),
                )
            if _delete_message_from_session(session_id, message_id):
                return Response(status_code=204)
            raise _message_not_found(message_id, session_id=session_id)
        for sid in list(app.state.messages):
            if _delete_message_from_session(sid, message_id):
                return Response(status_code=204)
        raise HTTPException(
            status_code=404,
            detail=ErrorEnvelope(
                error=ErrorInfo(
                    error="not_found",
                    message=f"message not found: {message_id}",
                    details={"message_id": message_id},
                    recoverable=False,
                )
            ).model_dump(exclude_none=True),
        )

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

    def _error_code_for_status(status_code: int) -> str:
        if status_code == 404:
            return "not_found"
        if status_code == 405:
            return "unsupported"
        if status_code in {400, 422}:
            return "validation_error"
        if status_code in {401, 403}:
            return "permission_error"
        return "internal_error" if status_code >= 500 else "request_error"

    @app.exception_handler(HTTPException)
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request, exc: StarletteHTTPException) -> JSONResponse:
        """Wrap HTTPExceptions in the v0.2 error envelope."""

        if isinstance(exc.detail, dict) and "error" in exc.detail:
            # Already an envelope (caller built one explicitly).
            return JSONResponse(status_code=exc.status_code, content=exc.detail)
        envelope = ErrorEnvelope(
            error=ErrorInfo(
                error=_error_code_for_status(exc.status_code),
                message=str(exc.detail) if exc.detail else "",
                recoverable=exc.status_code < 500,
            )
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope.model_dump(exclude_none=True),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(request, exc: RequestValidationError) -> JSONResponse:
        """Wrap FastAPI request validation failures in the GACT envelope."""

        envelope = ErrorEnvelope(
            error=ErrorInfo(
                error="validation_error",
                message="Request validation failed.",
                details={"errors": exc.errors()},
                recoverable=True,
            )
        )
        return JSONResponse(
            status_code=422,
            content=envelope.model_dump(exclude_none=True),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
        """Return a structured 500 for unexpected route failures."""

        envelope = ErrorEnvelope(
            error=ErrorInfo(
                error="internal_error",
                message="Unhandled server error.",
                details={
                    "original_error": type(exc).__name__,
                    "original_message": str(exc),
                },
                recoverable=False,
            )
        )
        return JSONResponse(
            status_code=500,
            content=envelope.model_dump(exclude_none=True),
        )

    # --- optional web UI (`clio web`): serve the built SPA bundle same-origin ---
    # Gated on CLIO_WEB_DIR so the default server (TUI / headless API) is byte-for-
    # byte unchanged unless web mode is explicitly enabled. Mounted LAST so every
    # /v1 API route (and /docs, /openapi.json) registered above takes precedence;
    # an SPA fallback serves index.html for unknown non-API paths so client-side
    # (history) routing works. The bundle's API calls are same-origin (relative
    # /v1/...), so no CORS/proxy is needed — this is the in-process equivalent of
    # the docker clio-web nginx setup.
    _web_dir = os.environ.get("CLIO_WEB_DIR", "").strip()
    if _web_dir and (Path(_web_dir) / "index.html").is_file():
        from fastapi.staticfiles import StaticFiles
        from starlette.responses import FileResponse

        class _SPAStaticFiles(StaticFiles):
            async def get_response(self, path: str, scope: Any) -> Any:
                try:
                    return await super().get_response(path, scope)
                except StarletteHTTPException as exc:
                    if exc.status_code == 404:
                        return FileResponse(Path(_web_dir) / "index.html")
                    raise

        app.mount("/", _SPAStaticFiles(directory=_web_dir, html=True), name="web")

    return app


# Module-level ``app`` for uvicorn-style invocations:
#   uvicorn clio_agent.gact.app:app
#
# Built lazily via PEP 562 module ``__getattr__`` so that ``import
# clio_agent.gact.app`` (which the ``clio-agent-gact`` console script
# triggers) doesn't pay build_app's cost — that includes pulling in
# clio_agent.tools.execution + litellm (~4 s on Aurora's frameworks
# Python). main() constructs its own app explicitly, so the only
# consumer of this attribute is the ``uvicorn …:app`` form, which
# always materialises it on first request anyway.
_lazy_app: Optional[FastAPI] = None


def __getattr__(name: str):
    global _lazy_app  # noqa: PLW0603
    if name == "app":
        if _lazy_app is None:
            _lazy_app = build_app()
        return _lazy_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    # gact-tui's `agent deploy` invokes adapters with --cwd; we don't
    # care about the value (CLIO reads file paths from CLIO_ALLOWED_ROOTS
    # / its own config), but the flag has to be accepted or argparse
    # bails with exit 2 and the deploy probe sees an instant zombie.
    parser.add_argument(
        "--cwd",
        default=None,
        help=(
            "ignored — accepted for compatibility with `gact agent "
            "deploy clio`, which always passes --cwd."
        ),
    )
    args = parser.parse_args()

    # Resolve trace verbosity (file→env→default) and install the formatted log
    # handler for the server process, now that the environment is settled.
    trace.configure()

    # Always build a fresh app inside main() — the module-level
    # ``app`` symbol is intentionally lazy (see __getattr__ above) so
    # that just importing ``clio_agent.gact.app`` doesn't pay
    # build_app's cost. When the env requests an agent we set
    # want_agent so the lifespan startup task constructs ClioAgent
    # in the background — uvicorn binds the port immediately, beating
    # gact-tui's 3-second deploy probe. POST /messages 503s until
    # app.state.agent is stamped by the background task.
    app_to_run: FastAPI = build_app()
    if not args.no_agent and os.environ.get("CLIO_LM_PROVIDER"):
        app_to_run.state.want_agent = True

    uvicorn.run(
        app_to_run,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
