# ruff: noqa: F821  (the kernel binds ``tools`` and ``ToolError`` into this module)
"""``stream``: write a stream many times a second from a script.

    s = await stream.open("printer", columns=["state", "nozzle_c"])
    while True:
        await s.write(state="printing", nozzle_c=214.5)
        await asyncio.sleep(1)

``open`` is one ``open_stream`` call (asked about like any call); each ``write`` then goes
straight to the stream, with no tool call of its own. A view bound to ``s.view_uri``
shows the latest rows while someone looks at it.
"""


class Stream:
    """One open stream: ``write`` rows to it, ``stop`` it."""

    def __init__(self, opened):
        self.name = opened["name"]
        self.view_uri = opened["view_uri"]
        self.window = opened["window"]
        self._id = opened["stream_id"]
        self._key = opened["write_key"]

    def __repr__(self):
        return f"<stream {self.name} -> {self.view_uri}>"

    async def write(self, row=None, **fields):
        """Add one row (a dict, or keyword fields); the oldest leaves past the window."""
        data = dict(row or {})
        data.update(fields)
        return await tools.call("__stream_write__", stream_id=self._id, key=self._key, rows=[data])

    async def write_many(self, rows):
        """Add several rows at once, the newest last."""
        return await tools.call(
            "__stream_write__", stream_id=self._id, key=self._key, rows=[dict(r) for r in rows]
        )

    async def stop(self):
        """Stop writing: the last rows stay shown, marked stopped."""
        return await tools.call("__stream_stop__", stream_id=self._id, key=self._key)


class _Streams:
    """Open streams: ``await stream.open(name, columns=None, window=1, flush_s=30)``."""

    async def open(self, name, columns=None, window=1, flush_s=30.0):
        opened = await tools.call(
            "open_stream", name=name, columns=list(columns or []), window=window, flush_s=flush_s
        )
        if not isinstance(opened, dict) or not opened.get("ok"):
            detail = opened.get("detail") if isinstance(opened, dict) else opened
            raise ToolError(f"open_stream: {detail}")
        return Stream(opened)


export = _Streams()
