# ruff: noqa: F821  (the kernel binds ``tools`` and ``ToolError`` into this module)
"""``stream``: write and read a stream from a script.

    s = await stream.open("sensor", window=60)
    while True:
        await s.write(t=time.time(), temperature_c=21.4)   # a JSON record
        await s.write("door opened")                      # a line of text
        await asyncio.sleep(1)

    latest = await stream.read("sensor", limit=10)        # state and newest entries

``open`` is one ``open_stream`` call (asked about like any call); each ``write`` then goes
straight to the stream in clio-core, with no tool call of its own. An entry is a dict (or
keyword fields), a str, or bytes with ``media_type`` (``s.write(png, media_type=
"image/png")``). The stream shows in the side panel and in any view bound to
``s.view_uri``. A write is sent when made; awaiting it waits for it, and a forgotten
``await`` still sends (the script ends after it is done).
"""


def _wire(value):
    """Bytes cross to the server as base64 (the call channel is JSON)."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        import base64

        return {"$bytes": base64.b64encode(bytes(value)).decode("ascii")}
    return value


class Stream:
    """One open stream: ``write`` entries to it, ``read`` it, ``stop`` it."""

    def __init__(self, opened):
        self.name = opened["name"]
        self.view_uri = opened["view_uri"]
        self.window = opened["window"]
        self._id = opened["stream_id"]
        self._key = opened["write_key"]

    def __repr__(self):
        return f"<stream {self.name} -> {self.view_uri}>"

    def write(self, value=None, media_type="", **fields):
        """Add one entry: a dict (or keyword fields), a str, or bytes with ``media_type``;
        the oldest leaves past the window."""
        if value is None:
            value = dict(fields)
        elif fields and isinstance(value, dict):
            value = {**value, **fields}
        return self.write_many([value], media_type=media_type)

    def write_many(self, values, media_type=""):
        """Add several entries at once, the newest last."""
        return tools.call(
            "__stream_write__",
            stream_id=self._id,
            key=self._key,
            values=[_wire(value) for value in values],
            media_type=media_type,
        )

    def read(self, limit=50):
        """This stream's state and newest entries (one ``read_stream`` call)."""
        return tools.call("read_stream", name=self.name, limit=limit)

    def stop(self):
        """Stop writing: the entries stay, shown as closed."""
        return tools.call("__stream_stop__", stream_id=self._id, key=self._key)


class _Streams:
    """``await stream.open(name, window=1, media_type="")``; ``await stream.read(name)``."""

    async def open(self, name, window=1, media_type=""):
        opened = await tools.call("open_stream", name=name, window=window, media_type=media_type)
        if not isinstance(opened, dict) or not opened.get("ok"):
            detail = opened.get("detail") if isinstance(opened, dict) else opened
            raise ToolError(f"open_stream: {detail}")
        return Stream(opened)

    def read(self, name="", limit=50):
        """A stream's state and newest entries; with no name, the workspace's streams."""
        return tools.call("read_stream", name=name, limit=limit)


export = _Streams()
