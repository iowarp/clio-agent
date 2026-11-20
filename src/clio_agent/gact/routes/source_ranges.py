"""``/v1/sessions/{sid}/source-ranges/{handle}``: a linked file's bytes, by HTTP ``Range``.

Loopback only: the handle is the capability, minted for one session by
``connected_data_open`` with ``range``. ``HEAD`` gives the size; ``GET`` with one
``bytes=`` range answers 206 with exactly those bytes, read through the source's adapter;
``/v1/sessions/{sid}/source-range-stats/{handle}`` reports the bytes and requests
served so far. A ``GET`` without ``Range`` streams the whole file in chunks: GDAL with
``CPL_VSIL_CURL_USE_HEAD=NO`` (the geoservices server) learns a file's size from such a
``GET``'s headers and then drops the connection, and refusing it (416) made GDAL report
every large tile as an unsupported format. A trailing file name after the handle must be the handle's own file:
readers such as GDAL probe sidecars by appending to the URL (``….tif.aux.xml``,
``….msk``), and answering those with the file's own bytes made GDAL parse a raster as
XML, hundreds of reads per open. Any other name is 404, as a missing sidecar is.

The handle without its file name is the file's folder to a reader, not the file: GDAL
lists it on open (``GET …/{handle}/``, redirected to ``…/{handle}``), and streaming the
whole file there made every open of a 3.3 GB raster pull all 3.3 GB first (eight
minutes per open; a basin read of three rasters ran 23). A ``GET`` there without
``Range`` is 404 (:data:`NOT_A_LISTING`), as an unlistable folder is; ranged reads of
it still answer.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse

from clio_agent.gact.auth import is_loopback_address
from clio_agent.gact.storage.range_handles import RangeHandle, registry

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
#: Typed 404 detail: the handle without its file name was read whole, as a folder listing.
NOT_A_LISTING = "range_handle_not_a_listing"
#: A read without ``Range`` up to this size is answered in one body; larger, it streams.
WHOLE_READ_LIMIT = 8 * 2**20
#: The chunk a streamed whole read fetches at a time.
STREAM_CHUNK = 2**20


def _handle(app: FastAPI, request: Request, sid: str, handle: str, name: str = "") -> RangeHandle:
    if not is_loopback_address(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="Byte ranges are served on loopback only")
    try:
        found = registry(app).get(sid, handle)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown range handle") from None
    except TimeoutError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from None
    if name and name != found.path.rsplit("/", 1)[-1]:
        raise HTTPException(status_code=404, detail="Not this handle's file")
    return found


def _span(header: str | None, size: int) -> tuple[int, int] | None:
    """``(start, end)`` inclusive for one satisfiable range, ``None`` for no header."""
    if header is None:
        return None
    match = _RANGE.match(header.strip())
    if match is None or (not match.group(1) and not match.group(2)):
        raise HTTPException(status_code=416, detail="Use one bytes=start-end range")
    first, last = match.groups()
    if not first:  # a suffix: the last n bytes
        start, end = max(0, size - int(last)), size - 1
    else:
        start = int(first)
        end = min(size - 1, int(last)) if last else size - 1
    if start >= size or start > end:
        raise HTTPException(
            status_code=416,
            detail="Range not satisfiable",
            headers={"Content-Range": f"bytes */{size}"},
        )
    return start, end


async def _stream(app: FastAPI, found: RangeHandle) -> AsyncIterator[bytes]:
    """The whole file, one chunk at a time; a client that disconnects stops the reads."""
    start = 0
    while start < found.size:
        data = await asyncio.to_thread(found.read, start, min(STREAM_CHUNK, found.size - start))
        if not data:
            return
        registry(app).served(found, len(data))
        start += len(data)
        yield data


def register_source_range_routes(app: FastAPI) -> None:
    """Serve minted range handles."""

    @app.head("/v1/sessions/{sid}/source-ranges/{handle}")
    @app.head("/v1/sessions/{sid}/source-ranges/{handle}/{name}")
    async def range_head(sid: str, handle: str, request: Request, name: str = "") -> Response:
        found = _handle(app, request, sid, handle, name)
        return Response(
            headers={
                "Content-Length": str(found.size),
                "Accept-Ranges": "bytes",
                "ETag": f'"{found.revision}"',
                "Content-Type": "application/octet-stream",
            }
        )

    @app.get("/v1/sessions/{sid}/source-ranges/{handle}")
    @app.get("/v1/sessions/{sid}/source-ranges/{handle}/{name}")
    async def range_get(sid: str, handle: str, request: Request, name: str = "") -> Response:
        found = _handle(app, request, sid, handle, name)
        span = _span(request.headers.get("range"), found.size)
        if span is None and not name:
            raise HTTPException(
                status_code=404,
                detail=f"{NOT_A_LISTING}: read the file at its named URL (handle_url)",
            )
        if span is None:
            if found.size > WHOLE_READ_LIMIT:
                return StreamingResponse(
                    _stream(app, found),
                    media_type="application/octet-stream",
                    headers={
                        "Content-Length": str(found.size),
                        "Accept-Ranges": "bytes",
                        "ETag": f'"{found.revision}"',
                    },
                )
            span, status = (0, max(0, found.size - 1)), 200
        else:
            status = 206
        start, end = span
        try:
            data = await asyncio.to_thread(found.read, start, end - start + 1)
        except ValueError as exc:  # source_revision_changed and other typed refusals
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from None
        registry(app).served(found, len(data))
        headers = {"Accept-Ranges": "bytes", "ETag": f'"{found.revision}"'}
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{start + len(data) - 1}/{found.size}"
        return Response(
            content=data, status_code=status, media_type="application/octet-stream", headers=headers
        )

    @app.get("/v1/sessions/{sid}/source-range-stats/{handle}")
    async def range_stats(sid: str, handle: str, request: Request) -> dict:
        return _handle(app, request, sid, handle).stats()
