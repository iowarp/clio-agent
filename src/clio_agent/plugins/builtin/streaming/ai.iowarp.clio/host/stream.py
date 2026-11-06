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


def _unwire(value: Any) -> Any:
    """A script's bytes arrive as ``{"$bytes": base64}`` (the call channel is JSON)."""
    if isinstance(value, Mapping) and set(value) == {"$bytes"}:
        import base64

        return base64.b64decode(str(value["$bytes"]))
    return value


@host_call("__stream_write__")
def write(args: Mapping[str, Any]) -> dict[str, Any]:
    stream = _store().write(
        str(args.get("stream_id") or ""),
        str(args.get("key") or ""),
        [_unwire(value) for value in args.get("values") or []],
        str(args.get("media_type") or ""),
    )
    return {"writes": stream.seq, "held": min(stream.seq, stream.window)}


@host_call("__stream_stop__")
def stop(args: Mapping[str, Any]) -> dict[str, Any]:
    stream = _store().stop(str(args.get("stream_id") or ""), str(args.get("key") or ""))
    return {"state": stream.state, "writes": stream.seq}
