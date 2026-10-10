"""The runtime's side of named instances and the model router (a mixin of the runtime).

* A named instance or the router launched with no port gets one the target's
  OS reports free, never one another deployment there has recorded.
* A vLLM launch is refused (typed) when the target's GPU shares would exceed 1.0.
* After an instance starts, stops or is removed, the running router is
  restarted with the new model list -- as ``model_router`` stop and start
  operations of its own, so the activity shows what happened and why. With no
  instance left, CLIO stops the router and remembers it did
  (``router.idle``); the next instance to start brings it back.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Literal

from clio_agent.gact.infrastructure.gpu_share import check_gpu_share
from clio_agent.gact.infrastructure.model_instances import (
    PORT_FIELD,
    ROUTER_SERVICE,
    allocate_port,
    needs_port,
    validate_service_id,
)
from clio_agent.gact.infrastructure.model_router import (
    DIGEST_FIELD,
    IDLE_FIELD,
    RouterInputs,
    config_digest,
    refreshes_router,
    routed_models,
    router_inputs,
)
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    InfrastructureOperation,
    ServiceActionRequest,
)
from clio_agent.gact.infrastructure.server_access import load_key
from clio_agent.gact.infrastructure.store import InfrastructureStore

if TYPE_CHECKING:
    from clio_agent.gact.infrastructure.models import ServiceRecord

logger = logging.getLogger(__name__)


class ModelRouterMixin:
    """Ports, GPU shares and router regeneration for the infrastructure runtime."""

    store: InfrastructureStore
    _execute: Callable[[str, CommandSpec], Awaitable[CommandResult]]
    _run_operation_locked: Callable[
        [InfrastructureOperation, ServiceActionRequest], Awaitable[None]
    ]

    def _admit(self, service_id: str, request: ServiceActionRequest) -> None:
        """Refuse, before queueing, an invalid instance id or a launch over the GPU budget."""

        validate_service_id(service_id)
        check_gpu_share(
            self.store,
            request.target_id,
            service_id,
            request.action,
            request.variant_id,
            request.configuration,
        )

    async def _prepare_launch(
        self, service_id: str, request: ServiceActionRequest
    ) -> ServiceActionRequest:
        """Check the GPU budget again under the target lock, and choose a port if needed."""

        check_gpu_share(
            self.store,
            request.target_id,
            service_id,
            request.action,
            request.variant_id,
            request.configuration,
        )
        if not needs_port(service_id, request.action, request.configuration):
            return request
        from clio_agent.gact.infrastructure.drivers import (  # noqa: PLC0415
            service_connection_port,
        )

        taken: set[int] = set()
        for record in self.store.services():
            if record.target_id != request.target_id or record.service_id == service_id:
                continue
            try:
                port = service_connection_port(
                    record.service_id, record.configuration, record.variant_id
                )
            except ValueError:
                continue
            if port:
                taken.add(port)
        port = await allocate_port(lambda spec: self._execute(request.target_id, spec), taken)
        return request.model_copy(
            update={"configuration": {**request.configuration, PORT_FIELD: str(port)}}
        )

    def _router_inputs(self, service_id: str, target_id: str) -> RouterInputs | None:
        """What a model-router launch routes to; ``None`` for every other service."""

        if service_id != ROUTER_SERVICE:
            return None
        return router_inputs(self.store, target_id, load_key)

    async def _refresh_router(self, target_id: str, service_id: str, action: str) -> None:
        """Restart the target's router when ``action`` changed the models it should serve.

        Never raises: the instance operation already succeeded; a router that
        could not follow has failed operations of its own in the activity.
        """

        if not refreshes_router(service_id, action):
            return
        router = self.store.service(target_id, ROUTER_SERVICE)
        if router is None:
            return
        idle = router.state == "stopped" and router.configuration.get(IDLE_FIELD) == "true"
        if router.state != "running" and not idle:
            return  # stopped by the person, or not installed: CLIO does not start it
        models = routed_models(self.store.services(), target_id)
        if router.state == "running" and config_digest(models) == router.configuration.get(
            DIGEST_FIELD
        ):
            return
        verbs: list[Literal["stop", "start"]] = []
        if router.state == "running":
            verbs.append("stop")
        if models:
            verbs.append("start")
        try:
            for verb in verbs:
                if not await self._router_operation(router, verb):
                    return
            if not models:
                current = self.store.service(target_id, ROUTER_SERVICE)
                if current is not None:
                    self.store.update_service(
                        target_id,
                        ROUTER_SERVICE,
                        configuration={**current.configuration, IDLE_FIELD: "true"},
                    )
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                "model router refresh failed: reason=router_refresh_error target=%s: %s",
                target_id,
                exc,
            )

    async def _router_operation(
        self, router: ServiceRecord, verb: Literal["start", "stop"]
    ) -> bool:
        """Run one router stop or start as its own operation; whether it succeeded."""

        row = self.store.put_operation(
            InfrastructureOperation(
                service_id=ROUTER_SERVICE,
                target_id=router.target_id,
                action=verb,
                progress="Updating the model router's models",
            )
        )
        await self._run_operation_locked(
            row,
            ServiceActionRequest(
                target_id=router.target_id, action=verb, variant_id=router.variant_id
            ),
        )
        finished = self.store.operation(row.id)
        if finished is None or finished.state != "succeeded":
            logger.warning(
                "model router refresh incomplete: reason=router_%s_failed target=%s",
                verb,
                router.target_id,
            )
            return False
        return True


__all__ = ["ModelRouterMixin"]
