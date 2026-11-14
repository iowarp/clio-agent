"""``/v1/sessions/{sid}/source-ranges/{handle}``: a linked file's bytes, by HTTP ``Range``.

Loopback only: the handle is the capability, minted for one session by
``connected_data_open`` with ``range``. ``HEAD`` gives the size; ``GET`` with one
``bytes=`` range answers 206 with exactly those bytes, read through the source's adapter;
``/v1/sessions/{sid}/source-range-stats/{handle}`` reports the bytes and requests
served so far. A trailing file name after the handle is accepted and ignored.
"""

from __future__ import annotations

import asyncio
import re

from fastapi import FastAPI, HTTPException, Request, Response

from clio_agent.gact.auth import is_loopback_address
from clio_agent.gact.storage.range_handles import RangeHandle, registry

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
#: A read without ``Range`` is served whole only up to this size.
WHOLE_READ_LIMIT = 8 * 2**20


def _handle(app: FastAPI, request: Request, sid: str, handle: str) -> RangeHandle:
    if not is_loopback_address(request.client.host if request.client else None):
        raise HTTPException(status_code=403, detail="Byte ranges are served on loopback only")
    try:
        return registry(app).get(sid, handle)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown range handle") from None
    except TimeoutError as exc:
        raise HTTPException(status_code=410, detail=str(exc)) from None


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


def register_source_range_routes(app: FastAPI) -> None:
    """Serve minted range handles."""

    @app.head("/v1/sessions/{sid}/source-ranges/{handle}")
    @app.head("/v1/sessions/{sid}/source-ranges/{handle}/{name}")
    async def range_head(sid: str, handle: str, request: Request, name: str = "") -> Response:
        found = _handle(app, request, sid, handle)
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
        found = _handle(app, request, sid, handle)
        span = _span(request.headers.get("range"), found.size)
        if span is None:
            if found.size > WHOLE_READ_LIMIT:
                raise HTTPException(
                    status_code=416,
                    detail="Request a byte range; this file is served by range only",
                    headers={"Content-Range": f"bytes */{found.size}"},
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
