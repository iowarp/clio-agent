"""``/v1/workspaces/{wid}/h5grove/*`` — HDF5 data for the workspace file viewer.

The gact-tui HDF5 viewer (h5web) reads files through h5grove's HTTP API: one
request per group listing, attribute set or array slice, so a large file is
never downloaded whole. The router itself is the HDF Group's
``hdf5_mcp.provider.routes`` from clio-kit's ``hdf5-mcp[data-routes]`` extra;
this module only mounts it and supplies the file resolver.

A ``file`` parameter resolves exactly as ``GET /v1/workspaces/{wid}/files/read``
resolves ``path`` (:func:`readable_workspace_file`): managed inputs, granted
folders, workspace containment, raw-read redaction, symlink and size policy,
with the same error envelopes. Bearer auth applies through the app-wide
middleware.

The route is optional. Without ``hdf5-mcp[data-routes]`` installed nothing is
mounted, and the viewer shows its "could not read" state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request

from clio_agent.gact.routes.workspace_file_read import readable_workspace_file

H5GROVE_PREFIX = "/v1/workspaces/{wid}/h5grove"


def workspace_h5grove_resolver(app: FastAPI) -> Callable[[str, Request], Any]:
    """Build the router's ``FileResolver``: the workspace comes from the mount prefix."""

    async def resolve(file: str, request: Request) -> Path:
        wid = request.path_params["wid"]
        return await asyncio.to_thread(readable_workspace_file, app, wid, file)

    return resolve


def register_workspace_h5grove_routes(app: FastAPI) -> bool:
    """Mount the h5grove data route when ``hdf5-mcp[data-routes]`` is installed.

    Returns whether the route was mounted.
    """

    try:
        from hdf5_mcp.provider.routes import create_router  # noqa: PLC0415
    except ImportError:
        return False
    app.include_router(create_router(workspace_h5grove_resolver(app)), prefix=H5GROVE_PREFIX)
    return True
