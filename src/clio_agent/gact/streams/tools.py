"""The stream tools the streaming plugin claims: open, read and stop a stream.

``open_stream`` is the one gated call a script makes before writing many times a second
through the plugin's ``stream`` binding; its result carries the key the binding's writes
present, and the ``view_uri`` a dashboard's chart or table binds to. ``read_stream`` gives
the agent (or a script) a stream's state and newest entries, or, with no name, the
workspace's streams; ``stop_stream`` closes a stream, whose entries stay, shown as closed.
The content is in clio-core (:mod:`clio_agent.gact.streams.store`).
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact.streams.store import WINDOW_MAX, StreamError, stream_store

STREAM_TOOLS = ("open_stream", "read_stream", "stop_stream")
_READ_ENTRIES = 50


def _session() -> tuple[Any, str, str]:
    from clio_agent.gact import context  # noqa: PLC0415
    from clio_agent.gact.artifacts.minting import _session_workspace_id  # noqa: PLC0415

    app, session_id = context.active_app(), context.active_session_id()
    if app is None or not session_id:
        raise RuntimeError("a stream needs an active session")
    return app, session_id, str(_session_workspace_id(app, session_id) or "")


def _refusal(exc: StreamError) -> dict[str, Any]:
    return {"ok": False, "reason": exc.code, "detail": str(exc)}


def open_stream(name: str, window: int = 1, media_type: str = "") -> dict[str, Any]:
    """Open a stream: a named artifact whose entries live in clio-core, written many times
    a second by a script (``s = await stream.open(name)`` then ``await s.write(...)`` in
    run_code) with no tool call per write. An entry is a JSON record or row (a dict), a
    line of text (a str), or bytes with a media_type. window keeps the newest N entries
    (1: the latest only). The stream shows in the side panel with its latest entries,
    live while open and closed after; a dashboard chart or table bound to the returned
    view_uri updates by itself while someone looks. A background script's streams close
    when it ends; their entries stay readable."""

    from clio_agent.gact import context  # noqa: PLC0415
    from clio_agent.gact.code_task_context import active_background_task  # noqa: PLC0415

    app, session_id, workspace_id = _session()
    task = active_background_task()
    try:
        stream = stream_store(app).open(
            session_id,
            workspace_id,
            name,
            window=int(window),
            media_type=str(media_type or ""),
            task=task.handle if task is not None else "",
            turn_id=context.active_turn_id(),
        )
    except StreamError as exc:
        return _refusal(exc)
    return {"ok": True, **stream.describe(), "write_key": stream.token}


def read_stream(name: str = "", limit: int = _READ_ENTRIES) -> dict[str, Any]:
    """Read a stream: its state (open, or closed and when), how many writes it had and
    how many entries it holds (``held``), and its newest ``limit`` entries (``entries``,
    oldest first; a record as ``value``, text as ``text``, other types by size;
    ``latest`` is the newest). With no name, list the workspace's streams and their
    states."""

    app, _session_id, workspace_id = _session()
    store = stream_store(app)
    try:
        if not name:
            return {"ok": True, "streams": [s.describe() for s in store.streams(workspace_id)]}
        stream = store.by_name(workspace_id, name)
        if stream is None:
            return {"ok": False, "reason": "stream_not_found", "detail": f"No stream {name!r}."}
        entries = store.read(stream, max(1, min(int(limit), WINDOW_MAX)))
        fresh = store.get(stream.id, workspace_id) or stream
    except StreamError as exc:
        return _refusal(exc)
    return {
        "ok": True,
        **fresh.describe(),
        "latest": entries[-1] if entries else None,
        "entries": entries,
    }


def stop_stream(name: str) -> dict[str, Any]:
    """Stop a stream this conversation writes: writes end; its entries stay, shown as
    closed."""

    app, session_id, workspace_id = _session()
    store = stream_store(app)
    try:
        stream = store.by_name(workspace_id, name)
        if stream is None:
            return {"ok": False, "reason": "stream_not_found", "detail": f"No stream {name!r}."}
        stopped = store.stop(stream.id, session_id=session_id)
    except StreamError as exc:
        return _refusal(exc)
    return {"ok": True, **stopped.describe()}


def _native(func: Any, fields: str, *, title: str, args: dict[str, Any], read_only: bool) -> Any:
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415

    return native_tool(
        func,
        name=func.__name__,
        title=title,
        desc=func.__doc__,
        args=args,
        presentation=f"fields:{fields}",
        domain="artifacts",
        read_only=read_only,
    )


_NAME = {"type": "string", "description": "The stream's name (its artifact's name)."}


def build_open_stream_tool() -> Any:
    return _native(
        open_stream,
        "name,state,view_uri,window,reason,detail",
        title="Open stream",
        args={
            "name": _NAME,
            "window": {"type": "integer", "minimum": 1, "maximum": WINDOW_MAX, "default": 1},
            "media_type": {
                "type": "string",
                "default": "",
                "description": "The entries' type when they are bytes (e.g. image/png).",
            },
        },
        read_only=False,
    )


def build_read_stream_tool() -> Any:
    return _native(
        read_stream,
        "name,state,latest,entries,held,writes,updated_at,closed_at,reason,detail",
        title="Read stream",
        args={
            "name": {**_NAME, "default": "", "description": "The stream; none lists them."},
            "limit": {"type": "integer", "minimum": 1, "default": _READ_ENTRIES},
        },
        read_only=True,
    )


def build_stop_stream_tool() -> Any:
    return _native(
        stop_stream,
        "name,state,writes,closed_at,reason,detail",
        title="Stop stream",
        args={"name": _NAME},
        read_only=False,
    )


BUILDERS = {
    "open_stream": build_open_stream_tool,
    "read_stream": build_read_stream_tool,
    "stop_stream": build_stop_stream_tool,
}

__all__ = ["BUILDERS", "STREAM_TOOLS", "open_stream", "read_stream", "stop_stream"]
