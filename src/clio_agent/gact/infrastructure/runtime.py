"""Async lifecycle orchestration for CLIO-owned infrastructure."""

from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from urllib.parse import urlsplit

import anyio
import httpx

from clio_agent.gact.infrastructure.clio_agent_deploy import ClaimResult, parse_claim
from clio_agent.gact.infrastructure.drivers import (
    LOOPBACK_ONLY_SERVICES,
    DriverPlan,
    build_driver_plan,
    service_connection_port,
    service_definitions,
)
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    ConnectionStrategy,
    ExternalServiceConnection,
    ExternalServiceConnectionRequest,
    InfrastructureOperation,
    ManagedServiceCatalog,
    ServiceActionRequest,
    ServiceRecord,
    ServiceState,
    TargetFacts,
    utc_now,
)
from clio_agent.gact.infrastructure.probe import probe_target
from clio_agent.gact.infrastructure.store import InfrastructureStore
from clio_agent.gact.infrastructure.transport import (
    InfrastructureTransportRegistry,
    TransportUnavailableError,
)
from clio_agent.providers.credentials import resolve as resolve_credential

MAX_OPERATION_LOG_CHARS = 16_000

logger = logging.getLogger(__name__)


def _bounded(value: str) -> str:
    if len(value) <= MAX_OPERATION_LOG_CHARS:
        return value
    return f"…{value[-MAX_OPERATION_LOG_CHARS:]}"


def _run_local(spec: CommandSpec) -> CommandResult:
    try:
        completed = subprocess.run(
            [spec.program, *spec.args],
            input=spec.stdin or None,
            text=True,
            capture_output=True,
            check=False,
            timeout=spec.timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        return CommandResult(exit_code=127, stderr=f"{spec.program} is not installed: {exc}")
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            exit_code=124,
            stdout=str(exc.stdout or ""),
            stderr=f"Command timed out after {spec.timeout_seconds:g} seconds",
        )
    return CommandResult(
        exit_code=completed.returncode,
        stdout=_bounded(completed.stdout),
        stderr=_bounded(completed.stderr),
    )


def _tcp_reachable(url: str) -> bool:
    parsed = urlsplit(url)
    if not parsed.hostname:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=2):
            return True
    except OSError:
        return False


class InfrastructureRuntime:
    """Own service operations and delegate only byte transport to Desktop."""

    def __init__(
        self,
        store: InfrastructureStore,
        transports: InfrastructureTransportRegistry,
        *,
        local_executor: Callable[[CommandSpec], Awaitable[CommandResult]] | None = None,
        credential_resolver: Callable[[str, str], str] = resolve_credential,
        endpoint_reachable: Callable[[str], Awaitable[bool]] | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self.transports = transports
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._target_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._local_executor = local_executor or self._execute_local
        self._credential_resolver = credential_resolver
        self._endpoint_reachable = endpoint_reachable or self._check_tcp_reachable
        self._http_transport = http_transport

    async def _check_tcp_reachable(self, url: str) -> bool:
        return await anyio.to_thread.run_sync(_tcp_reachable, url)

    async def _execute_local(self, spec: CommandSpec) -> CommandResult:
        return await anyio.to_thread.run_sync(_run_local, spec)

    async def _execute(self, target_id: str, spec: CommandSpec) -> CommandResult:
        target = self.store.target(target_id)
        if target is None:
            raise KeyError(target_id)
        if target.kind == "direct":
            raise ValueError("Direct endpoints are connection-only and have no lifecycle catalog")
        if spec.scope == "controller" or target.kind == "local":
            return await self._local_executor(spec)
        if target.kind != "ssh":
            raise ValueError("Connection-only targets do not support lifecycle commands")
        return await self.transports.execute(target_id, spec)

    async def catalog(self, target_id: str) -> ManagedServiceCatalog:
        """Inspect one target and merge its durable service records."""

        target = self.store.target(target_id)
        if target is None:
            raise KeyError(target_id)
        if target.kind == "direct":
            raise ValueError("Direct endpoints are connection-only and have no lifecycle catalog")

        async def execute(spec: CommandSpec) -> CommandResult:
            return await self._execute(target_id, spec)

        facts = await probe_target(target, execute if target.kind == "ssh" else None)
        services = service_definitions(facts)
        for service in services:
            record = self.store.service(target_id, service.id)
            if record is None:
                service.state = "not_installed"
                continue
            service.state = await self._reconcile_service(target_id, record, facts)
            refreshed = self.store.service(target_id, service.id) or record
            if service.state == "running" and (port := service_connection_port(service.id)):
                try:
                    url, strategy = await self._resolve_connection(
                        target_id,
                        port,
                        service_id=service.id,
                        previous_url=refreshed.connection_url,
                        previous_strategy=refreshed.connection_strategy,
                    )
                    if (url, strategy) != (
                        refreshed.connection_url,
                        refreshed.connection_strategy,
                    ):
                        refreshed = self.store.put_service(
                            refreshed.model_copy(
                                update={"connection_url": url, "connection_strategy": strategy}
                            )
                        )
                except (OSError, RuntimeError, ValueError):
                    pass
            service.connection_url = refreshed.connection_url
            service.connection_strategy = refreshed.connection_strategy
        return ManagedServiceCatalog(facts=facts, services=services)

    async def _reconcile_service(
        self, target_id: str, record: ServiceRecord, facts: TargetFacts
    ) -> ServiceState:
        """Observe actual service state without replaying any lifecycle action."""

        try:
            plan = build_driver_plan(
                service_id=record.service_id,
                action="status",
                variant_id=record.variant_id,
                configuration=record.configuration,
                facts=facts,
                target=self.store.target(target_id),
            )
            output: list[str] = []
            for spec in plan.commands:
                result = await self._execute(target_id, spec)
                output.extend(part for part in (result.stdout, result.stderr) if part)
                if result.exit_code not in spec.allowed_exit_codes:
                    return "unknown"
            observed = "\n".join(output).casefold()
            state: ServiceState = (
                "running"
                if "running" in observed
                else "stopped"
                if any(value in observed for value in ("exited", "created", "stopped"))
                else "unknown"
            )
            if state != record.state:
                self.store.put_service(record.model_copy(update={"state": state}))
            return state
        except (OSError, RuntimeError, ValueError):
            return "unknown"

    def start_action(
        self, service_id: str, request: ServiceActionRequest
    ) -> InfrastructureOperation:
        """Start one operation and return its durable receipt immediately."""

        if self.store.target(request.target_id) is None:
            raise KeyError(request.target_id)
        row = self.store.put_operation(
            InfrastructureOperation(
                service_id=service_id,
                target_id=request.target_id,
                action=request.action,
            )
        )
        task = asyncio.create_task(self._run_operation(row, request))
        self._tasks[row.id] = task

        def forget_task(_task: asyncio.Task[None], operation_id: str = row.id) -> None:
            self._tasks.pop(operation_id, None)

        task.add_done_callback(forget_task)
        return row

    async def cancel(self, operation_id: str) -> InfrastructureOperation:
        """Cancel a running operation without claiming its remote effects rolled back."""

        row = self.store.operation(operation_id)
        if row is None:
            raise KeyError(operation_id)
        task = self._tasks.get(operation_id)
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        current = self.store.operation(operation_id) or row
        if current.state in {"queued", "running"}:
            current = self.store.put_operation(
                current.model_copy(
                    update={
                        "state": "cancelled",
                        "progress": "Cancelled; inspect actual service state before retrying.",
                    }
                )
            )
        return current

    async def _run_operation(
        self,
        row: InfrastructureOperation,
        request: ServiceActionRequest,
    ) -> None:
        async with self._target_locks[request.target_id]:
            await self._run_operation_locked(row, request)

    async def _run_operation_locked(
        self,
        row: InfrastructureOperation,
        request: ServiceActionRequest,
    ) -> None:
        row = self.store.put_operation(
            row.model_copy(update={"state": "running", "progress": "Inspecting target"})
        )
        plan: DriverPlan | None = None
        claim: ClaimResult | None = None
        try:
            catalog = await self.catalog(request.target_id)
            definition = next(
                (item for item in catalog.services if item.id == row.service_id), None
            )
            if definition is None:
                raise ValueError(f"Unknown managed service {row.service_id!r}")
            row = self.store.put_operation(
                row.model_copy(update={"progress": f"Running {request.action}"})
            )
            plan = build_driver_plan(
                service_id=row.service_id,
                action=request.action,
                variant_id=request.variant_id,
                configuration=request.configuration,
                facts=catalog.facts,
                target=self.store.target(request.target_id),
            )
            output: list[str] = []
            for spec in plan.commands:
                result = await self._execute(request.target_id, spec)
                output.extend(part for part in (result.stdout, result.stderr) if part)
                row = self.store.put_operation(
                    row.model_copy(update={"logs": _bounded("\n".join(output))})
                )
                if result.exit_code not in spec.allowed_exit_codes:
                    raise RuntimeError(
                        result.stderr.strip()
                        or result.stdout.strip()
                        or f"{spec.program} exited with code {result.exit_code}"
                    )
                claim = parse_claim(result.stdout) or claim
                if claim is not None and claim.result == "adopted":
                    # The healthy server of this exact install and version
                    # keeps running; installing or starting again is not needed.
                    break
                if spec.settle_seconds:
                    await asyncio.sleep(spec.settle_seconds)
            await self._settle_service(row.service_id, request, plan.connection_port, output)
            self.store.put_operation(
                row.model_copy(
                    update={
                        "state": "succeeded",
                        "progress": "Completed",
                        "logs": _bounded("\n".join(output)),
                        "error": None,
                    }
                )
            )
        except asyncio.CancelledError:
            cleanup = await self._teardown(request.target_id, plan, claim)
            self.store.put_operation(
                row.model_copy(
                    update={
                        "state": "cancelled",
                        "progress": f"Cancelled. {cleanup}",
                    }
                )
            )
            raise
        except (KeyError, OSError, RuntimeError, ValueError) as exc:
            cleanup = await self._teardown(request.target_id, plan, claim)
            self.store.put_operation(
                row.model_copy(
                    update={"state": "failed", "progress": f"Failed. {cleanup}", "error": str(exc)}
                )
            )

    async def _teardown(
        self, target_id: str, plan: DriverPlan | None, claim: ClaimResult | None
    ) -> str:
        """Undo what a failed or cancelled plan started; report what happened.

        Runs only after the claim step: before it, this plan started nothing,
        and an adopted server was already running, so it is left alone.
        """

        if plan is None or plan.teardown is None or claim is None or claim.result == "adopted":
            return "Nothing this deploy started needed cleaning up."
        try:
            result = await self._execute(target_id, plan.teardown(claim))
        except (OSError, RuntimeError, TransportUnavailableError) as exc:
            logger.warning(
                "infrastructure teardown failed: reason=teardown_error target=%s", target_id
            )
            return f"Cleanup failed: {exc}"
        if result.exit_code != 0:
            return f"Cleanup failed: {(result.stderr or result.stdout).strip()}"
        return "Cleaned up what this deploy started."

    async def _settle_service(
        self,
        service_id: str,
        request: ServiceActionRequest,
        connection_port: int | None,
        output: list[str],
    ) -> None:
        previous = self.store.service(request.target_id, service_id)
        if request.action == "uninstall":
            self.store.delete_service(request.target_id, service_id)
            return
        state: ServiceState = previous.state if previous else "unknown"
        if request.action in {"install", "reinstall", "start"}:
            state = "running"
        elif request.action == "stop":
            state = "stopped"
        elif request.action == "status" and output:
            observed = output[-1].strip().casefold()
            state = (
                "running"
                if "running" in observed
                else "stopped"
                if "exited" in observed
                else "unknown"
            )
        connection_url = previous.connection_url if previous else None
        strategy: ConnectionStrategy | None = previous.connection_strategy if previous else None
        if connection_port and state == "running":
            try:
                connection_url, strategy = await self._resolve_connection(
                    request.target_id,
                    connection_port,
                    service_id=service_id,
                    previous_url=connection_url,
                    previous_strategy=strategy,
                    wait_for_direct=request.action in {"install", "reinstall", "start"},
                )
            except (OSError, RuntimeError, ValueError):
                connection_url = previous.connection_url if previous else None
                strategy = previous.connection_strategy if previous else None
        self.store.put_service(
            ServiceRecord(
                id=f"{request.target_id}:{service_id}",
                service_id=service_id,
                target_id=request.target_id,
                variant_id=request.variant_id,
                configuration=request.configuration,
                state=state,
                connection_url=connection_url,
                connection_strategy=strategy,
            )
        )

    async def _resolve_connection(
        self,
        target_id: str,
        port: int,
        *,
        service_id: str,
        previous_url: str | None = None,
        previous_strategy: ConnectionStrategy | None = None,
        wait_for_direct: bool = False,
    ) -> tuple[str, ConnectionStrategy]:
        """Where the desktop reaches a running service, and how.

        A service that listens on the target's loopback only
        (:data:`LOOPBACK_ONLY_SERVICES`) is never tried at the host's address:
        that attempt cannot succeed, and on a route through jump hosts it
        spent half a minute on retries before the forward even started.
        """
        target = self.store.target(target_id)
        if target is None:
            raise KeyError(target_id)
        if target.kind == "local":
            loopback = f"http://127.0.0.1:{port}"
            if wait_for_direct:
                for attempt in range(10):
                    if await self._endpoint_reachable(loopback):
                        break
                    if attempt < 9:
                        await asyncio.sleep(0.5)
            return loopback, "loopback"
        if target.kind != "ssh" or target.ssh is None:
            raise ValueError("Managed services require a local or SSH target")
        host = target.ssh.host.strip()
        if host and service_id not in LOOPBACK_ONLY_SERVICES:
            direct = f"http://{host}:{port}"
            attempts = 10 if wait_for_direct else 1
            for attempt in range(attempts):
                if await self._endpoint_reachable(direct):
                    return direct, "direct"
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.5)
        preferred_port = None
        if previous_strategy == "ssh_forward" and previous_url:
            parsed = urlsplit(previous_url)
            if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
                preferred_port = parsed.port
        return await self.transports.forward(target_id, port, preferred_port), "ssh_forward"

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
