"""Hooks contributed by a session's resolved plugins.

A plugin ships hooks as SDK ``@hook`` functions (``ai.iowarp.clio/hooks/*.py``)
or, when imported from a Claude Code plugin, as ``hooks/hooks.json`` commands.
Resolution orders them child to parent and by ``before``/``after``; the
dispatcher runs a session's plugin hooks in that order, ahead of the
user/project/managed ``hooks.json`` entries. Plugin hooks were trusted with
their plugin (the marketplace trust step), so they carry ``trust="trusted"``.
"""

from __future__ import annotations

import threading
from typing import Any

from clio_agent.gact.hooks.adapters import HookAdapter
from clio_agent.gact.hooks.config import HookEntry, HookMatch, HookRun, _compile_anchored
from clio_agent.gact.hooks.events import ANSWER
from clio_agent.gact.hooks.wire import HookDecision, HookEnvelope, HookInfraError
from clio_agent.runtime import trace

_CACHE: dict[str, list[HookEntry]] = {}
_LOCK = threading.Lock()


def _entry(row: dict[str, Any]) -> HookEntry:
    tools = str(row.get("tools") or "")
    if row["kind"] == "python":
        run = HookRun(
            type="python", command=str(row["root"]), args=(str(row["module"]), str(row["attr"]))
        )
    else:
        dialect = "claude-code" if row.get("dialect") == "claude-code" else "command"
        run = HookRun(type=dialect, command=str(row["command"]), args=tuple(row.get("args") or ()))
    return HookEntry(
        id=str(row["id"]),
        on=frozenset({str(row["event"])}),
        match=HookMatch(tool=_compile_anchored(tools, hook_id=str(row["id"])) if tools else None),
        run=run,
        timeout_ms=int(row.get("timeout_ms") or 30000),
        fail_closed=bool(row.get("fail_closed")),
        source=str(row.get("root") or ""),
        scope=f"plugin:{row.get('plugin', '')}",
        trust="trusted",
    )


def plugin_hook_entries(session_id: str) -> list[HookEntry]:
    """The ordered hook entries of ``session_id``'s pinned resolution."""

    if not session_id:
        return []
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        session_resolution,
    )

    # The dispatcher is process-wide; a session's plugins are the active app's.
    app = _ctx.active_app()
    if app is None or getattr(getattr(app, "state", None), "plugin_service", None) is None:
        return []
    try:
        resolution = session_resolution(app, session_id)
    except SessionAgentUnavailable as exc:
        trace.event(
            "HOOKS",
            "plugin_hooks_unavailable reason=%s session=%s agent=%s",
            exc.reason,
            session_id,
            exc.ref,
        )
        return []
    with _LOCK:
        cached = _CACHE.get(resolution.digest)
    if cached is not None:
        return cached
    entries = [_entry(row) for row in resolution.hooks]
    with _LOCK:
        _CACHE[resolution.digest] = entries
    return entries


class PythonHookAdapter(HookAdapter):
    """Run an SDK ``@hook`` function in process.

    The function receives a :class:`clio_agent.sdk.HookEvent`. It returns
    ``None`` to allow, or a mapping with ``decision``, ``reason`` and
    ``additional_context`` (an ``answer`` hook adds ``note``, ``check`` and
    ``answer``); raising :class:`PermissionError` denies with its message. Any
    other exception is a typed hook infrastructure failure.
    """

    def invoke(
        self,
        entry: HookEntry,
        envelope: HookEnvelope,
        *,
        cancel_event: "threading.Event | None" = None,
    ) -> HookDecision:
        del cancel_event
        from clio_agent.plugins.loader import load_attribute  # noqa: PLC0415
        from clio_agent.sdk.hooks import HookEvent, HookSpec  # noqa: PLC0415

        module, attr = entry.run.args
        try:
            spec = load_attribute(entry.run.command, module, attr)
        except Exception as exc:  # noqa: BLE001 - typed infra failure
            raise HookInfraError(
                "hook_crashed", f"hook {entry.id!r} could not load: {exc}", hook_id=entry.id
            ) from exc
        function = spec.function if isinstance(spec, HookSpec) else spec
        event = HookEvent(
            event=envelope.hook_event_name,
            session_id=envelope.session_id,
            turn_id=envelope.turn_id,
            cwd=envelope.cwd,
            tool_name=envelope.tool_name,
            tool_input=envelope.tool_input,
            prompt=envelope.prompt,
            payload=envelope.payload,
            answer=(envelope.payload or {}).get("answer")
            if envelope.hook_event_name == ANSWER
            else None,
        )
        try:
            result = function(event)
        except PermissionError as exc:
            return HookDecision(
                decision="deny", reason=str(exc) or "denied by hook", hook_id=entry.id
            )
        except Exception as exc:  # noqa: BLE001 - typed infra failure, never a user rejection
            raise HookInfraError(
                "hook_crashed",
                f"hook {entry.id!r} raised {type(exc).__name__}: {exc}",
                hook_id=entry.id,
            ) from exc
        if result is None:
            return HookDecision(decision="allow", hook_id=entry.id)
        if isinstance(result, HookDecision):
            return result
        if isinstance(result, dict):
            decision = str(result.get("decision") or "allow")
            if decision not in ("allow", "deny"):
                raise HookInfraError(
                    "hook_crashed",
                    f"hook {entry.id!r} returned unknown decision {decision!r}",
                    hook_id=entry.id,
                )
            check = result.get("check")
            return HookDecision(
                decision=decision,  # type: ignore[arg-type]
                reason=str(result.get("reason") or ""),
                additional_context=str(result.get("additional_context") or ""),
                hook_id=entry.id,
                note=str(result.get("note") or ""),
                check=check if isinstance(check, dict) else None,
                updated_output=str(result.get("answer") or ""),
            )
        raise HookInfraError(
            "hook_crashed", f"hook {entry.id!r} returned {type(result).__name__}", hook_id=entry.id
        )
