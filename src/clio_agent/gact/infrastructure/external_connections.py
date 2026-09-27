"""Connection-only services: endpoints CLIO checks but whose lifecycle it does not own.

A mixin of :class:`~clio_agent.gact.infrastructure.runtime.InfrastructureRuntime`
(kept apart so the runtime stays an orchestrator of managed lifecycles).
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from clio_agent.gact.infrastructure.models import (
    ExternalServiceConnection,
    ExternalServiceConnectionRequest,
    utc_now,
)
from clio_agent.gact.infrastructure.store import InfrastructureStore


class ExternalConnectionsMixin:
    """Create, edit and health-check connection-only service endpoints."""

    store: InfrastructureStore
    _credential_resolver: Callable[[str, str], str]
    _http_transport: httpx.AsyncBaseTransport | None

    async def create_external_connection(
        self,
        request: ExternalServiceConnectionRequest,
    ) -> ExternalServiceConnection:
        """Persist and health-check a connection whose lifecycle is external."""

        url = str(request.url).rstrip("/")
        reachable = await self._external_reachable(url, request.credential_ref)
        return self.store.put_connection(
            ExternalServiceConnection(
                service_id=request.service_id,
                label=request.label,
                url=url,
                credential_ref=request.credential_ref,
                reachable=reachable,
                checked_at=utc_now(),
            )
        )

    async def update_external_connection(
        self,
        connection_id: str,
        request: ExternalServiceConnectionRequest,
    ) -> ExternalServiceConnection:
        """Edit and recheck a connection-only service without gaining lifecycle ownership."""

        previous = self.store.connection(connection_id)
        if previous is None:
            raise KeyError(connection_id)
        url = str(request.url).rstrip("/")
        reachable = await self._external_reachable(url, request.credential_ref)
        return self.store.put_connection(
            previous.model_copy(
                update={
                    "service_id": request.service_id,
                    "label": request.label,
                    "url": url,
                    "credential_ref": request.credential_ref,
                    "reachable": reachable,
                    "checked_at": utc_now(),
                }
            )
        )

    async def check_external_connection(self, connection_id: str) -> ExternalServiceConnection:
        """Refresh health for an external endpoint without changing its definition."""

        row = self.store.connection(connection_id)
        if row is None:
            raise KeyError(connection_id)
        reachable = await self._external_reachable(row.url, row.credential_ref)
        return self.store.put_connection(
            row.model_copy(update={"reachable": reachable, "checked_at": utc_now()})
        )

    async def _external_reachable(self, url: str, credential_ref: str) -> bool:
        token = (
            self._credential_resolver("infrastructure", credential_ref) if credential_ref else ""
        )
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=5.0,
                transport=self._http_transport,
            ) as client:
                response = await client.get(url, headers=headers)
            return response.status_code < 500 and response.status_code not in {401, 403}
        except httpx.HTTPError:
            return False
