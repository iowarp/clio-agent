"""Byte-range reads of an attached NDP resource, so a script reads a window, not the file.

A range handle (:mod:`clio_agent.gact.storage.range_handles`) serves a linked file to
GDAL, h5py or xarray, which ask for the few ranges they need: a COG's header and the
tiles of one window, a NetCDF variable's chunks. NDP resources are HTTP(S) objects,
signed redirects or Endpoint routes; each read here is one GET with ``Range`` on the
same route, sign-in and redirect checks as a whole-file read
(:meth:`~clio_agent.gact.storage.ndp_source.NDPSource._send`).

A server that ignores ``Range`` and answers 200 with the whole object is refused with
:data:`~clio_agent.gact.storage.range_handles.RANGE_UNSUPPORTED` (the reason names the
server's answer), never read whole in its place: the agent then downloads the file
deliberately, within its size limit.
"""

from __future__ import annotations

from typing import Callable

import httpx

#: One ranged GET: headers in, the open response and its client out (caller closes).
Send = Callable[[dict[str, str]], tuple[httpx.Response, httpx.Client]]


def _refused(status: int) -> str:
    from clio_agent.gact.storage.range_handles import RANGE_UNSUPPORTED  # noqa: PLC0415

    return (
        f"{RANGE_UNSUPPORTED}: this NDP server answered a byte-range request with HTTP "
        f"{status}, not a range; download the file instead"
    )


class NDPRangeReader:
    """Exact byte ranges of one NDP resource, counted."""

    def __init__(self, send: Send, size: int) -> None:
        self.send, self.size = send, size
        self.requests = 0
        self.bytes_read = 0

    def total_size(self) -> int:
        """The object's size: the catalog's, or the total of a one-byte ranged answer."""
        if not self.size:
            response, client = self.send({"Range": "bytes=0-0"})
            try:
                total = response.headers.get("content-range", "").rpartition("/")[2]
                if response.status_code != 206 or not total.isdigit():
                    raise ValueError(_refused(response.status_code))
            finally:
                response.close()
                client.close()
            self.size = int(total)
        return self.size

    def read_range(self, start: int, length: int) -> bytes:
        """``length`` bytes from ``start`` (fewer at the end of the object)."""
        if length <= 0 or (self.size and start >= self.size):
            return b""
        end = start + length - 1
        if self.size:
            end = min(end, self.size - 1)
        response, client = self.send({"Range": f"bytes={start}-{end}"})
        try:
            if response.status_code != 206:
                raise ValueError(_refused(response.status_code))
            data = b"".join(response.iter_bytes(chunk_size=65536))
        except httpx.HTTPError as exc:
            raise ValueError("NDP stopped streaming this range; retry the read") from exc
        finally:
            response.close()
            client.close()
        data = data[: end - start + 1]
        self.requests += 1
        self.bytes_read += len(data)
        return data
