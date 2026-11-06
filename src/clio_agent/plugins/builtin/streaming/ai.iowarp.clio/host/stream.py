"""The server side of the ``stream`` binding: writes and stops without a tool call each.

A write presents the key ``open_stream`` returned, so only a script that opened the
stream through that (gated, recorded) call can write to it.
"""

from collections.abc import Mapping
from typing import Any

from clio_agent.sdk import host_call


def _store() -> Any:
    from clio_agent.gact import context
    from clio_agent.gact.streams.store import stream_store

    app = context.active_app()
    if app is None:
        raise RuntimeError("no active server for this stream write")
    return stream_store(app)


@host_call("__stream_write__")
def write(args: Mapping[str, Any]) -> dict[str, Any]:
    rows = args.get("rows")
    stream = _store().write(
        str(args.get("stream_id") or ""), str(args.get("key") or ""), list(rows or [])
    )
    return {"writes": stream.seq, "rows": len(stream.rows)}


@host_call("__stream_stop__")
def stop(args: Mapping[str, Any]) -> dict[str, Any]:
    stream = _store().stop(str(args.get("stream_id") or ""), str(args.get("key") or ""))
    return {"state": stream.state, "writes": stream.seq}
