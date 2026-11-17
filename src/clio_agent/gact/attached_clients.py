"""Clients attached to this server, counted like clio-core counts its clients.

Every program using this server registers when it attaches and releases when it
closes: a script's ``ClioClient``, ``clio-agent -q``, the desktop app. A client on
this machine gives its process id; one that exits without releasing is pruned when
its process is gone (checked against its creation time, so a reused id is not
mistaken for it).

A server a script started (``clio_agent.serve.ensure_server`` sets
:data:`STOP_WITH_LAST_CLIENT_ENV`) stops itself when its last client leaves, by
release or by exit: the normal graceful shutdown (turn drain, clio-core release),
the same as ``POST /v1/server/shutdown``. Any other server (``clio-agent serve``,
``clio start``, the desktop's own) only counts.

Routes (loopback needs no token; elsewhere the server's bearer applies, as for every
route):

* ``POST /v1/server/clients`` ``{"kind", "pid", "label"}`` → ``{"client_id", "attached"}``
* ``DELETE /v1/server/clients/{client_id}`` → ``{"attached", "stopping"}``
* ``GET /v1/server/clients`` → ``{"attached", "clients", "stop_with_last_client"}``
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo

logger = logging.getLogger(__name__)

#: Set (to ``1``) on a server a script started: it stops when its last client leaves.
STOP_WITH_LAST_CLIENT_ENV = "CLIO_STOP_WITH_LAST_CLIENT"
#: How often clients whose process is gone are pruned.
PRUNE_INTERVAL_S = 2.0


class AttachRequest(BaseModel):
    """A client registering: what it is and, on this machine, its process."""

    kind: str = Field(default="sdk", max_length=40)
    pid: int | None = Field(default=None, ge=1)
    label: str = Field(default="", max_length=200)


def stops_with_last_client() -> bool:
    """Whether this server stops itself when its last client leaves."""

    return os.environ.get(STOP_WITH_LAST_CLIENT_ENV) == "1"


def _create_time(pid: int) -> float | None:
    import psutil  # noqa: PLC0415

    try:
        return float(psutil.Process(pid).create_time())
    except psutil.Error:  # no such process, or access denied: not ours to track
        return None


def _alive(row: dict[str, Any]) -> bool:
    pid, created = row.get("pid"), row.get("create_time")
    if pid is None or created is None:
        return True  # a remote client is counted until it releases
    current = _create_time(int(pid))
    return current is not None and abs(current - float(created)) < 1.0


class AttachedClients:
    """The registry on ``app.state.attached_clients``."""

    def __init__(self, app: FastAPI) -> None:
        self.app = app
        self.rows: dict[str, dict[str, Any]] = {}
        self.stopping = False
        self._pruner: asyncio.Task[None] | None = None

    def attach(self, request: AttachRequest) -> dict[str, Any]:
        client_id = "client_" + uuid.uuid4().hex[:16]
        created = _create_time(request.pid) if request.pid is not None else None
        if request.pid is not None and created is None:
            raise ValueError(f"process {request.pid} is not running on this server's machine")
        self.rows[client_id] = {
            "client_id": client_id,
            "kind": request.kind,
            "pid": request.pid,
            "create_time": created,
            "label": request.label,
            "attached_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("client attached: %s (%d attached)", self.rows[client_id], len(self.rows))
        if self._pruner is None or self._pruner.done():
            self._pruner = asyncio.get_running_loop().create_task(self._prune_loop())
        return {"client_id": client_id, "attached": len(self.rows)}

    def release(self, client_id: str) -> dict[str, Any]:
        row = self.rows.pop(client_id, None)
        if row is None:
            raise KeyError(client_id)
        logger.info("client released: %s (%d attached)", client_id, len(self.rows))
        return {"attached": len(self.rows), "stopping": self._stop_if_last("released")}

    def view(self) -> dict[str, Any]:
        return {
            "attached": len(self.rows),
            "clients": list(self.rows.values()),
            "stop_with_last_client": stops_with_last_client(),
            "stopping": self.stopping,
        }

    def dead(self) -> list[str]:
        """Clients whose process is gone (reads process tables: call off the loop)."""

        return [cid for cid, row in list(self.rows.items()) if not _alive(row)]

    def prune(self, gone: list[str]) -> None:
        """Drop the ``gone`` clients; stop when the last one left a script's server."""

        dropped = [cid for cid in gone if self.rows.pop(cid, None) is not None]
        for cid in dropped:
            logger.warning("client %s exited without releasing; pruned", cid)
        if dropped:
            self._stop_if_last("client_exited")

    def _stop_if_last(self, why: str) -> bool:
        if self.rows or self.stopping or not stops_with_last_client():
            return self.stopping
        from clio_agent.gact import desktop_lifecycle  # noqa: PLC0415

        self.stopping = True
        logger.warning("last client left (%s): stopping this script-started server", why)
        desktop_lifecycle.request_managed_shutdown(self.app, "serve")
        return True

    async def _prune_loop(self) -> None:
        while self.rows and not self.stopping:
            await asyncio.sleep(PRUNE_INTERVAL_S)
            try:
                gone = await asyncio.to_thread(self.dead)
            except Exception:  # noqa: BLE001 - logged; the next sweep retries
                logger.exception("attached-client sweep failed; retrying next interval")
                continue
            self.prune(gone)


def registry(app: FastAPI) -> AttachedClients:
    """The app's attached-client registry (created on first use)."""

    found = getattr(app.state, "attached_clients", None)
    if found is None:
        found = app.state.attached_clients = AttachedClients(app)
    return found


def _error(status: int, error: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail=ErrorEnvelope(
            error=ErrorInfo(error=error, message=message, recoverable=True)
        ).model_dump(exclude_none=True),
    )


def register_attached_client_routes(app: FastAPI) -> None:
    """Register the attach / release / list routes."""

    @app.post("/v1/server/clients", status_code=201)
    async def attach_client(request: AttachRequest) -> dict[str, Any]:
        clients = registry(app)
        if clients.stopping:
            raise _error(503, "server_stopping", "this server is stopping; start another")
        try:
            return clients.attach(request)
        except ValueError as exc:
            raise _error(422, "client_process_unknown", str(exc)) from exc

    @app.delete("/v1/server/clients/{client_id}")
    async def release_client(client_id: str) -> dict[str, Any]:
        try:
            return registry(app).release(client_id)
        except KeyError as exc:
            raise _error(404, "not_found", f"no attached client {client_id}") from exc

    @app.get("/v1/server/clients")
    async def list_clients() -> dict[str, Any]:
        return registry(app).view()


__all__ = [
    "STOP_WITH_LAST_CLIENT_ENV",
    "AttachRequest",
    "AttachedClients",
    "register_attached_client_routes",
    "registry",
    "stops_with_last_client",
]
