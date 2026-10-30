"""``/v1/workspaces/{wid}/h5grove/*`` — HDF5 data for the workspace file viewer.

The gact-tui HDF5 viewer (h5web) reads files through h5grove's HTTP API: one
request per group listing, attribute set or array slice, so a large file is
never downloaded whole. The endpoints are h5grove's (``meta``, ``attr``,
``data``, ``stats``, ``paths``), served from h5grove's content and encoders;
:mod:`clio_agent.gact.hdf5_view` resolves paths inside the file so that links
and virtual datasets cannot read outside the workspace.

A ``file`` parameter resolves exactly as ``GET /v1/workspaces/{wid}/files/read``
resolves ``path`` (:func:`readable_workspace_file`): managed inputs, granted
folders, workspace containment, raw-read redaction and symlink policy, with the
same error envelopes. The whole-file size cap does not apply, because only the
requested piece is read; instead each data or stats response is bounded by the
same policy cap. Bearer auth applies through the app-wide middleware.

h5py and h5grove load on the first request, not with the app: server startup must
not pay for HDF5 (``tests/test_tools/test_lazy_imports.py``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, Response

from clio_agent.gact.routes.workspace_file_read import readable_workspace_file
from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.runtime import trace
from clio_agent.tools.file_policy import FileAccessPolicy

H5GROVE_PREFIX = "/v1/workspaces/{wid}/h5grove"


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=ErrorEnvelope(
            error=ErrorInfo(error=code, message=message, recoverable=False)
        ).model_dump(exclude_none=True),
    )


def register_workspace_h5grove_routes(app: FastAPI) -> None:
    """Mount the h5grove endpoints the workspace HDF5 viewer reads."""

    def serve(wid: str, file: str, endpoint: str, **params: Any) -> Response:
        from clio_agent.gact import hdf5_view  # noqa: PLC0415 - h5py loads per request

        target = readable_workspace_file(app, wid, file, whole_file=False)
        workspace = app.state.workspaces.get(wid)
        root = Path(workspace.root_path or os.getcwd()).expanduser().resolve()
        roots = hdf5_view.Roots((root, target.parent))
        try:
            body, headers = hdf5_view.respond(
                target, roots, endpoint, params, _max_response_bytes()
            )
        except hdf5_view.Hdf5ViewError as exc:
            return _error(exc.status, exc.code, exc.message)
        return Response(content=body, headers=headers)

    @app.api_route(f"{H5GROVE_PREFIX}/", methods=["GET", "HEAD"])
    def h5grove_root(wid: str) -> Response:
        return Response("ok")

    @app.get(f"{H5GROVE_PREFIX}/meta")
    def h5grove_meta(wid: str, file: str, path: str = "/") -> Response:
        return serve(wid, file, "meta", path=path)

    @app.get(f"{H5GROVE_PREFIX}/attr")
    def h5grove_attr(
        wid: str, file: str, path: str = "/", attr_keys: Annotated[list[str] | None, Query()] = None
    ) -> Response:
        return serve(wid, file, "attr", path=path, attr_keys=attr_keys)

    @app.get(f"{H5GROVE_PREFIX}/data")
    def h5grove_data(
        wid: str,
        file: str,
        path: str = "/",
        dtype: str = "origin",
        format: str = "json",
        flatten: bool = False,
        selection: str | None = None,
    ) -> Response:
        return serve(
            wid,
            file,
            "data",
            path=path,
            dtype=dtype,
            format=format,
            flatten=flatten,
            selection=selection,
        )

    @app.get(f"{H5GROVE_PREFIX}/stats")
    def h5grove_stats(
        wid: str, file: str, path: str = "/", selection: str | None = None
    ) -> Response:
        return serve(wid, file, "stats", path=path, selection=selection)

    @app.get(f"{H5GROVE_PREFIX}/paths")
    def h5grove_paths(wid: str, file: str, path: str = "/") -> Response:
        return serve(wid, file, "paths", path=path)


def _max_response_bytes() -> int:
    """The policy cap that bounds a whole-file read bounds one slice here."""

    try:
        return FileAccessPolicy.from_mapping(os.environ).max_file_size_bytes
    except (ValueError, TypeError, OSError) as exc:
        trace.event("WORKSPACE", "file policy unavailable for HDF5 slices (%s); using 1 GiB", exc)
        return 1024**3
