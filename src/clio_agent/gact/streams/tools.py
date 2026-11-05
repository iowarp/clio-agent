"""The stream tools the streaming plugin claims: open, read and stop a stream.

``open_stream`` is the one gated call a script makes before writing many times a second
through the plugin's ``stream`` binding (CT25 (6)); its result carries the key the
binding's writes present, and the ``view_uri`` a dashboard's chart or table binds to.
``read_stream`` gives the agent the latest rows; ``stop_stream`` ends the writes and keeps
the last rows, shown as stopped.
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact.streams.store import StreamError, stream_store

STREAM_TOOLS = ("open_stream", "read_stream", "stop_stream")
_READ_ROWS = 50


def _session() -> tuple[Any, str, str]:
    from clio_agent.gact import context  # noqa: PLC0415
    from clio_agent.gact.artifacts.minting import _session_workspace_id  # noqa: PLC0415

    app, session_id = context.active_app(), context.active_session_id()
    if app is None or not session_id:
        raise RuntimeError("a stream needs an active session")
    return app, session_id, str(_session_workspace_id(app, session_id) or "")


def _refusal(exc: StreamError) -> dict[str, Any]:
    return {"ok": False, "reason": exc.code, "detail": str(exc)}


def open_stream(
    name: str, columns: list[str] | None = None, window: int = 1, flush_s: float = 30.0
) -> dict[str, Any]:
    """Open a stream: a named table whose latest rows live in memory, written many times a
    second by a script (``stream.open(name)`` then ``await s.write(row)`` in run_code) with
    no tool call per write. A dashboard's chart or table bound to the returned view_uri
    updates by itself, only while someone is looking. window keeps the last N rows (1: the
    latest only); the rows are saved as the artifact's next version every flush_s seconds
    while they change and when the stream stops. columns names the columns a view can bind
    to before the first write. A background script's streams stop when it ends; their
    last rows stay readable and show as stopped."""

    from clio_agent.gact.code_task_context import active_background_task  # noqa: PLC0415

    app, session_id, workspace_id = _session()
    task = active_background_task()
    try:
        stream = stream_store(app).open(
            session_id,
            workspace_id,
            name,
            columns=list(columns or []),
            window=int(window),
            flush_s=float(flush_s),
            task=task.handle if task is not None else "",
        )
    except StreamError as exc:
        return _refusal(exc)
    return {"ok": True, **stream.describe(), "write_key": stream.token}


def read_stream(name: str, limit: int = _READ_ROWS) -> dict[str, Any]:
    """Read a stream's latest rows (newest last) and its state: open with the time of the
    last write, or stopped. For a stream this server no longer holds (it restarted), the
    rows are in its artifact's last version (read it as a file or table)."""

    app, _session_id, workspace_id = _session()
    stream = stream_store(app).by_name(workspace_id, name)
    if stream is None:
        return {
            "ok": False,
            "reason": "stream_not_found",
            "detail": f"No stream {name!r} is held by this server; its saved rows, if any, "
            f"are the artifact {name.removesuffix('.parquet')}.parquet.",
        }
    rows = stream.snapshot()[-max(1, int(limit)) :]
    return {
        "ok": True,
        **stream.describe(),
        "latest": rows[-1] if rows else None,
        "rows_read": rows,
    }


def stop_stream(name: str) -> dict[str, Any]:
    """Stop a stream this conversation writes: writes end, the last rows are saved as the
    artifact's version and stay shown, marked stopped."""

    app, session_id, workspace_id = _session()
    store = stream_store(app)
    stream = store.by_name(workspace_id, name)
    if stream is None:
        return {"ok": False, "reason": "stream_not_found", "detail": f"No stream {name!r}."}
    try:
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


_NAME = {"type": "string", "description": "The stream's name (its artifact is <name>.parquet)."}


def build_open_stream_tool() -> Any:
    return _native(
        open_stream,
        "name,state,view_uri,window,flush_s,reason,detail",
        title="Open stream",
        args={
            "name": _NAME,
            "columns": {"type": "array", "items": {"type": "string"}, "default": []},
            "window": {"type": "integer", "minimum": 1, "default": 1},
            "flush_s": {"type": "number", "minimum": 0, "default": 30.0},
        },
        read_only=False,
    )


def build_read_stream_tool() -> Any:
    return _native(
        read_stream,
        "name,state,rows,writes,updated_at,stopped_at,reason,detail",
        title="Read stream",
        args={"name": _NAME, "limit": {"type": "integer", "minimum": 1, "default": _READ_ROWS}},
        read_only=True,
    )


def build_stop_stream_tool() -> Any:
    return _native(
        stop_stream,
        "name,state,writes,stopped_at,reason,detail",
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
