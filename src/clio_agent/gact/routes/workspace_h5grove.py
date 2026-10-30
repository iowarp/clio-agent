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
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, Response
from h5grove.content import DatasetContent, ResolvedEntityContent
from h5grove.encoders import encode
from h5grove.utils import NotFoundError, QueryArgumentError, hdf_path_join

from clio_agent.gact.hdf5_view import Hdf5ViewError, Roots, check_readable, open_view
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


def _encoded(content: Any, encoding: str | None) -> Response:
    try:
        encoded = encode(content, encoding)
    except QueryArgumentError as exc:
        raise Hdf5ViewError(422, "hdf5_invalid_query", str(exc)) from exc
    return Response(content=encoded.content, headers=encoded.headers)


def register_workspace_h5grove_routes(app: FastAPI) -> None:
    """Mount the h5grove endpoints the workspace HDF5 viewer reads."""

    def located(wid: str, file: str) -> tuple[Path, Roots]:
        target = readable_workspace_file(app, wid, file, whole_file=False)
        workspace = app.state.workspaces.get(wid)
        root = Path(workspace.root_path or os.getcwd()).expanduser().resolve()
        return target, Roots((root, target.parent))

    def serve(wid: str, file: str, read: Any) -> Response:
        try:
            target, roots = located(wid, file)
            with open_view(target, roots) as walk:
                return read(walk, roots)
        except Hdf5ViewError as exc:
            return _error(exc.status, exc.code, exc.message)
        except NotFoundError as exc:
            return _error(404, "hdf5_path_not_found", str(exc))
        except (QueryArgumentError, ValueError, TypeError) as exc:
            return _error(422, "hdf5_invalid_query", str(exc))

    def resolved(walk: Any, path: str) -> ResolvedEntityContent:
        content = walk.content(path)
        if not isinstance(content, ResolvedEntityContent):
            raise Hdf5ViewError(
                403, "hdf5_link_outside_workspace", f"{path} is a link that cannot be followed"
            )
        return content

    def dataset(walk: Any, path: str, selection: str | None, roots: Roots) -> DatasetContent:
        content = resolved(walk, path)
        if not isinstance(content, DatasetContent):
            raise Hdf5ViewError(422, "hdf5_not_a_dataset", f"{path} is not a dataset")
        check_readable(content._h5py_entity, selection, roots, _max_response_bytes())
        return content

    @app.api_route(f"{H5GROVE_PREFIX}/", methods=["GET", "HEAD"])
    def h5grove_root(wid: str) -> Response:
        return Response("ok")

    @app.get(f"{H5GROVE_PREFIX}/meta")
    def h5grove_meta(wid: str, file: str, path: str = "/") -> Response:
        return serve(wid, file, lambda walk, _: _encoded(walk.content(path).metadata(), "json"))

    @app.get(f"{H5GROVE_PREFIX}/attr")
    def h5grove_attr(
        wid: str, file: str, path: str = "/", attr_keys: Annotated[list[str] | None, Query()] = None
    ) -> Response:
        return serve(
            wid, file, lambda walk, _: _encoded(resolved(walk, path).attributes(attr_keys), "json")
        )

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
        def read(walk: Any, roots: Roots) -> Response:
            data = dataset(walk, path, selection, roots).data(selection, flatten, dtype)
            return _encoded(data, format)

        return serve(wid, file, read)

    @app.get(f"{H5GROVE_PREFIX}/stats")
    def h5grove_stats(
        wid: str, file: str, path: str = "/", selection: str | None = None
    ) -> Response:
        def read(walk: Any, roots: Roots) -> Response:
            return _encoded(dataset(walk, path, selection, roots).data_stats(selection), "json")

        return serve(wid, file, read)

    @app.get(f"{H5GROVE_PREFIX}/paths")
    def h5grove_paths(wid: str, file: str, path: str = "/") -> Response:
        def read(walk: Any, _: Roots) -> Response:
            group = resolved(walk, path)._h5py_entity
            if not hasattr(group, "id") or not hasattr(group.id, "links"):
                raise Hdf5ViewError(422, "hdf5_not_a_group", f"{path} is not a group")
            names = [path]
            # Link names only: listing never follows a link, so nothing leaves the file.
            group.id.links.visit(lambda name: names.append(hdf_path_join(path, name.decode())))
            return _encoded(names, "json")

        return serve(wid, file, read)


def _max_response_bytes() -> int:
    """The policy cap that bounds a whole-file read bounds one slice here."""

    try:
        return FileAccessPolicy.from_mapping(os.environ).max_file_size_bytes
    except (ValueError, TypeError, OSError) as exc:
        trace.event("WORKSPACE", "file policy unavailable for HDF5 slices (%s); using 1 GiB", exc)
        return 1024**3
