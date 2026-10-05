"""Browser folder selection using the existing SFTP adapter on the connected CLIO."""

from __future__ import annotations

import asyncio
import posixpath
import stat
from typing import Any

import paramiko
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from clio_agent.gact.infrastructure.models import SshRoute
from clio_agent.gact.storage.models import SftpCredentials
from clio_agent.gact.storage.sftp import SftpSource


class InspectSftp(BaseModel):
    """Inspect one saved host or test a nonsecret draft before saving it."""

    model_config = ConfigDict(extra="forbid")
    target_id: str = ""
    route: SshRoute | None = None
    path: str = Field(default="", max_length=8192)
    credentials: SftpCredentials | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def choose_one_host(self) -> InspectSftp:
        """Do not let a supplied route override the selected saved host."""
        if bool(self.target_id) == bool(self.route):
            raise ValueError("Choose one saved host or enter an SSH address")
        return self


def inspect_sftp(
    route: SshRoute, path: str, credentials: SftpCredentials | None = None
) -> dict[str, Any]:
    """List directories only, without reading file content or requiring remote Python."""
    source = SftpSource.from_route(route, path or ".", credentials=credentials)
    try:
        entries: list[dict[str, str]] = []
        truncated = False
        for entry in source.fs.ftp.listdir_iter(source.root):
            if not stat.S_ISDIR(entry.st_mode or 0):
                continue
            if len(entries) >= 200:
                truncated = True
                break
            entries.append(
                {"name": entry.filename, "path": posixpath.join(source.root, entry.filename)}
            )
        return {
            "path": source.root,
            "parent": posixpath.dirname(source.root.rstrip("/")) or "/",
            "entries": sorted(entries, key=lambda entry: entry["name"].casefold()),
            "truncated": truncated,
        }
    finally:
        source.close()


def register_storage_sftp_routes(app: FastAPI) -> None:
    """Reuse the authenticated API and durable infrastructure host definitions."""

    @app.post("/v1/storage/ssh/inspect")
    async def inspect(body: InspectSftp) -> dict[str, Any]:
        route = body.route
        if body.target_id:
            target = app.state.infrastructure_store.target(body.target_id)
            if target is None or target.kind != "ssh" or target.ssh is None:
                raise HTTPException(404, "SSH host not found")
            route = target.ssh
        assert route is not None  # Enforced by the request model.
        try:
            return await asyncio.to_thread(
                inspect_sftp,
                route,
                body.path,
                body.credentials,
            )
        except (OSError, ValueError, RuntimeError, paramiko.SSHException) as exc:
            raise HTTPException(409, str(exc)) from exc
