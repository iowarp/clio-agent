"""Async lifecycle orchestration for CLIO-owned infrastructure."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import socket
import subprocess
from collections import defaultdict
from collections.abc import Awaitable, Callable
from contextlib import suppress
from urllib.parse import urlsplit

import httpx
from anyio.to_thread import run_sync

from clio_agent.gact.infrastructure.clio_agent_deploy import ClaimResult, parse_claim
from clio_agent.gact.infrastructure.deployment_ledger import (
    forget_created,
    held_elsewhere,
    persist_created,
    remove_created,
)
from clio_agent.gact.infrastructure.drivers import (
    LOOPBACK_ONLY_SERVICES,
    DriverPlan,
    build_driver_plan,
    clio_agent_version,
    service_connection_port,
    service_definitions,
)
from clio_agent.gact.infrastructure.external_connections import ExternalConnectionsMixin
from clio_agent.gact.infrastructure.models import (
    CommandResult,
    CommandSpec,
    ConnectionStrategy,
    InfrastructureOperation,
    ManagedServiceCatalog,
    OwnedResource,
    ServiceActionRequest,
    ServiceRecord,
    ServiceState,
    TargetFacts,
    VersionConflictDetail,
)
from clio_agent.gact.infrastructure.probe import probe_target
from clio_agent.gact.infrastructure.remote_lifecycle import RemoteLaunch, stop_desktop_launches
from clio_agent.gact.infrastructure.resource_ledger import merge as merge_owned
from clio_agent.gact.infrastructure.resource_ledger import unheld
from clio_agent.gact.infrastructure.server_access import (
    ServerAccessMixin,
    forget_key,
    launch_key,
    load_key,
    retire_saved_servers,
    settle_failed_launch,
    store_key,
)
from clio_agent.gact.infrastructure.service_observation import parse_observation
from clio_agent.gact.infrastructure.service_readiness import observe_service, wait_until_ready
from clio_agent.gact.infrastructure.service_settlement import settle_service
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
            encoding="utf-8",
            errors="replace",
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


class InfrastructureRuntime(ExternalConnectionsMixin, ServerAccessMixin):
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
        self._remote_launches: dict[str, RemoteLaunch] = {}
        self._exiting_desktops: set[str] = set()

    async def stop_desktop_agents(self, desktop_id: str) -> list[str]:
        """Stop this Desktop's launches while its SSH bridges can still carry commands."""

        self._exiting_desktops.add(desktop_id)
        return await stop_desktop_launches(
            desktop_id, self._remote_launches, self.store, self._execute, self._target_locks
        )

    async def _check_tcp_reachable(self, url: str) -> bool:
        return await run_sync(_tcp_reachable, url)

    async def _execute_local(self, spec: CommandSpec) -> CommandResult:
        return await run_sync(_run_local, spec)

    async def execute_on_target(self, target_id: str, spec: CommandSpec) -> CommandResult:
        """Execute a server-generated host operation through the owning transport."""
        return await self._execute(target_id, spec)

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
            port = self._record_port(refreshed)
            if service.state == "running" and port:
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
                        refreshed = (
                            self.store.update_service(
                                target_id,
                                service.id,
                                connection_url=url,
                                connection_strategy=strategy,
                            )
                            or refreshed
                        )
                except (OSError, RuntimeError, ValueError):
                    pass
            if service.state == "running" and not refreshed.effective_parameters:
                refreshed = await self._refresh_effective(target_id, refreshed)
            service.connection_url = refreshed.connection_url
            service.connection_strategy = refreshed.connection_strategy
            service.configuration = dict(refreshed.configuration)
            service.owned_resources = list(refreshed.owned_resources)
            service.access = refreshed.access
            service.observation = refreshed.observation
            service.recommended_variant = refreshed.variant_id
            service.effective_parameters = (
                list(refreshed.effective_parameters) if service.state == "running" else []
            )
        return ManagedServiceCatalog(facts=facts, services=services)

    @staticmethod
    def _record_port(record: ServiceRecord) -> int | None:
        try:
            return service_connection_port(
                record.service_id, record.configuration, record.variant_id
            )
        except ValueError:
            return None

    async def _refresh_effective(self, target_id: str, record: ServiceRecord) -> ServiceRecord:
        """Read what a running model server has in force and keep it on its record."""

        async def execute(spec: CommandSpec) -> CommandResult:
            return await self._execute(target_id, spec)

        try:
            effective = await observe_service(record, execute, http_transport=self._http_transport)
        except (OSError, RuntimeError, ValueError) as exc:
            logger.warning(
                "infrastructure effective parameters unavailable: reason=observe_failed "
                "service=%s target=%s: %s",
                record.service_id,
                target_id,
                exc,
            )
            return record
        if not effective:
            return record
        return (
            self.store.update_service(target_id, record.service_id, effective_parameters=effective)
            or record
        )

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
                owned=record.owned_resources,
                resolved_root=record.resolved_root or None,
            )
            output: list[str] = []
            for spec in plan.commands:
                result = await self._execute(target_id, spec)
                output.extend(part for part in (result.stdout, result.stderr) if part)
                if result.exit_code not in spec.allowed_exit_codes:
                    return "unknown"
            structured = parse_observation(output)
            if structured is not None:
                self.store.update_service(
                    target_id,
                    record.service_id,
                    state=structured.service_state,
                    observation=structured,
                )
                return structured.service_state
            observed = "\n".join(output).casefold()
            state: ServiceState = (
                "running"
                if "running" in observed
                else "stopped"
                if any(value in observed for value in ("exited", "created", "stopped"))
                else "unknown"
            )
            # This variant's status carries no structured observation (a
            # container), so any stored one describes an earlier deployment
            # and would contradict ``state`` (F032).
            if state != record.state or record.observation is not None:
                self.store.update_service(
                    target_id, record.service_id, state=state, observation=None
                )
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
        created: list[OwnedResource] = []
        installed: ServiceRecord | None = None
        target_os = "linux"
        # Set only when a `connect` answer adopts a `clio_agent` under a root
        # other than the target's configured one (see the "found" handling
        # below); threaded into `_settle_service` so it is what gets persisted.
        resolved_root_update: str | None = None
        # The deployment key this operation made (install / reinstall), and the
        # one it replaces: a failure puts the right one back.
        previous_key = ""
        made_key = False
        try:
            catalog = await self.catalog(request.target_id)
            target_os = catalog.facts.os
            definition = next(
                (item for item in catalog.services if item.id == row.service_id), None
            )
            if definition is None:
                raise ValueError(f"Unknown managed service {row.service_id!r}")
            row = self.store.put_operation(
                row.model_copy(update={"progress": f"Running {request.action}"})
            )
            installed = self.store.service(request.target_id, row.service_id)
            if installed is not None and request.action in {"install", "reinstall"}:
                previous_directory = installed.configuration.get("storage.service_directory")
                requested_directory = request.configuration.get("storage.service_directory")
                if (
                    previous_directory
                    and requested_directory
                    and requested_directory != previous_directory
                ):
                    raise ValueError(
                        "This deployment owns its existing storage directory. Changing its path "
                        "requires an explicit data migration; reinstall cannot move or abandon it."
                    )
                if row.service_id in {"flowcept", "cmf"} and previous_directory:
                    for field, default in (
                        ("container_runtime", "docker"),
                        ("image_storage", "engine"),
                    ):
                        previous = installed.configuration.get(field) or default
                        requested = request.configuration.get(field) or previous
                        if requested != previous:
                            raise ValueError(
                                "This deployment owns its container runtime and image storage. "
                                "Remove its runtime and explicitly delete retained data before "
                                "choosing a different engine or image store."
                            )
            # `on_conflict` answers exactly THIS operation's found-conflict
            # question, if any. It must never be read back from a persisted
            # record or merged forward -- a past "Replace"/"Connect" would
            # otherwise silently decide a later, unrelated conflict (#1528
            # review) -- so it is captured once here and stripped from every
            # configuration dict before it can reach a merge, a driver plan's
            # `configuration`, or `_settle_service`'s persisted record. A
            # fresh claim runs, and can find (and ask about) a new mismatch,
            # on every single install/reinstall/start.
            on_conflict = request.configuration.get("on_conflict")
            if "on_conflict" in request.configuration:
                request = request.model_copy(
                    update={
                        "configuration": {
                            k: v for k, v in request.configuration.items() if k != "on_conflict"
                        }
                    }
                )
            target_row = self.store.target(request.target_id)
            if installed is not None and request.action != "install":
                # Lifecycle actions operate on what is installed: its variant,
                # negotiated runtime and parameters, not the form's current
                # state (empty after a reload). A reinstall keeps the installed
                # variant and lets values the person typed override it.
                typed = {k: v for k, v in request.configuration.items() if v.strip()}
                installed_configuration = {
                    k: v for k, v in installed.configuration.items() if k != "on_conflict"
                }
                request = request.model_copy(
                    update={
                        "variant_id": installed.variant_id,
                        "configuration": (
                            {**installed_configuration, **typed}
                            if request.action == "reinstall"
                            else {**request.configuration, **installed_configuration}
                        ),
                    }
                )
            api_key = launch_key(
                request.target_id, row.service_id, request.action, request.configuration
            )
            plan = build_driver_plan(
                service_id=row.service_id,
                action=request.action,
                variant_id=request.variant_id,
                configuration=request.configuration,
                facts=catalog.facts,
                target=target_row,
                owned=unheld(
                    installed.owned_resources if installed else [],
                    held_elsewhere(self.store, request.target_id, row.service_id),
                ),
                api_key=api_key,
                on_conflict=on_conflict,
                resolved_root=(installed.resolved_root or None) if installed else None,
            )
            if api_key and request.action in {"install", "reinstall"}:
                previous_key = load_key(request.target_id, row.service_id)
                store_key(request.target_id, row.service_id, api_key)
                made_key = True
            output: list[str] = []
            for index, spec in enumerate(plan.commands):
                if plan.remote_launch and spec.args[1].startswith("# clio-deploy:start"):
                    if plan.remote_launch.desktop_id in self._exiting_desktops:
                        raise RuntimeError("Desktop is closing; remote deployment was cancelled")
                    self._remote_launches[request.target_id] = plan.remote_launch
                result = await self._execute(request.target_id, spec)
                output.extend(part for part in (result.stdout, result.stderr) if part)
                row = self.store.put_operation(
                    row.model_copy(update={"logs": _bounded("\n".join(output))})
                )
                if (recorder := plan.recorders.get(index)) is not None:
                    # Before the exit check: a failed `run` can still leave a
                    # created container. Durable at once, so a CLIO that stops
                    # mid-deploy still names everything for uninstall.
                    created = merge_owned(created, recorder(result))
                    persist_created(
                        self.store,
                        row.service_id,
                        request,
                        plan.configuration or request.configuration,
                        installed,
                        created,
                    )
                if result.exit_code not in spec.allowed_exit_codes:
                    raise RuntimeError(
                        result.stderr.strip()
                        or result.stdout.strip()
                        or f"{spec.program} exited with code {result.exit_code}"
                    )
                claim = parse_claim(result.stdout) or claim
                if claim is not None and claim.result == "found":
                    if on_conflict == "connect":
                        # The person chose to connect to the running CLIO
                        # as-is; treat it like an exact-match adopt. If it
                        # lives under a different root than this target's
                        # configured one, remember the real root so later
                        # stop/logs/uninstall act on the process actually
                        # adopted, not on a fresh install's root.
                        if (
                            row.service_id == "clio_agent"
                            and claim.owner
                            and target_row is not None
                            and claim.owner.strip() != target_row.install_root.strip()
                        ):
                            resolved_root_update = claim.owner.strip()
                        break
                    # Nothing was stopped or installed. Surface what was
                    # found so the caller can ask "Connect" or "Replace"
                    # instead of clio silently deciding either way. A
                    # process that never answered its health check is never
                    # a reason to stop it either: `health` says so, typed,
                    # rather than clio guessing it is hung.
                    health = claim.health or "unknown"
                    progress = (
                        (
                            f"CLIO {claim.installed_version or '(unknown version)'} "
                            f"is already running on this host (pid {claim.pid or 'unknown'})."
                        )
                        if health == "healthy"
                        else (
                            f"A CLIO-looking process is already on this host's port "
                            f"(pid {claim.pid or 'unknown'}), but it isn't answering."
                        )
                    )
                    self.store.put_operation(
                        row.model_copy(
                            update={
                                "state": "failed",
                                "progress": progress,
                                "error": "clio_deploy_version_conflict",
                                "conflict": VersionConflictDetail(
                                    installed_version=claim.installed_version or "unknown",
                                    pid=claim.pid or "",
                                    health=health,
                                    target_version=clio_agent_version(),
                                    owner=claim.owner or "",
                                    port=plan.connection_port or 17800,
                                ),
                                "logs": _bounded("\n".join(output)),
                            }
                        )
                    )
                    return
                if claim is not None and claim.result == "adopted":
                    # The healthy server of this exact install and version
                    # keeps running; installing or starting again is not needed.
                    break
                if spec.settle_seconds:
                    await asyncio.sleep(spec.settle_seconds)

            def report(message: str, current: InfrastructureOperation = row) -> None:
                self.store.put_operation(current.model_copy(update={"progress": message}))

            async def execute(spec: CommandSpec) -> CommandResult:
                return await self._execute(request.target_id, spec)

            if plan.readiness is not None:
                await wait_until_ready(plan.readiness, execute, report)
            for spec in plan.after_ready:
                row = self.store.put_operation(
                    row.model_copy(update={"progress": "Preparing the model"})
                )
                result = await self._execute(request.target_id, spec)
                output.extend(part for part in (result.stdout, result.stderr) if part)
                if result.exit_code not in spec.allowed_exit_codes:
                    raise RuntimeError(
                        result.stderr.strip()
                        or result.stdout.strip()[-2000:]
                        or f"{spec.program} exited with code {result.exit_code}"
                    )
            if plan.after_ready_hook is not None:
                chosen = await plan.after_ready_hook(execute, report)
                base = plan.configuration or request.configuration
                plan = dataclasses.replace(plan, configuration={**base, **chosen})
            # A reinstall replaces only the server; the image and caches stay
            # on the ledger, so both install and reinstall merge.
            owned = merge_owned(installed.owned_resources if installed else [], created)
            if row.service_id == "clio_agent" and request.action in {
                "install",
                "start",
                "reinstall",
            }:
                adopted = claim is not None and claim.result in {"found", "adopted"}
                configuration = dict(request.configuration)
                configuration.update(
                    desktop_owned="false" if adopted else "true",
                    version=(claim.installed_version or "unknown")
                    if adopted and claim is not None
                    else clio_agent_version(),
                )
                request = request.model_copy(update={"configuration": configuration})
                if not adopted and plan.remote_launch:
                    resolved_root_update = plan.remote_launch.root
            await self._settle_service(
                row.service_id,
                request.model_copy(
                    update={"configuration": plan.configuration or request.configuration}
                ),
                plan.connection_port,
                output,
                owned,
                resolved_root=resolved_root_update,
                retain_record=plan.retain_record,
            )
            if request.action in {"uninstall", "delete_data"} and installed is not None:
                # The saved "Use in Models" entry goes with the deployment it names.
                retire_saved_servers(self.store, installed)
            if request.action in {"uninstall", "delete_data"} or (
                request.action in {"install", "reinstall"} and not api_key
            ):
                # Uninstall removes the key; a keyless (shareable) install drops an old one.
                forget_key(request.target_id, row.service_id)
            await self._record_access(request.target_id, row.service_id, plan.connection_port)
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
            cleanup = await self._teardown(request.target_id, plan, claim, created, target_os)
            if created and cleanup.startswith("Removed the"):
                forget_created(self.store, request.target_id, row.service_id, installed)
            if made_key:
                settle_failed_launch(
                    request.target_id,
                    row.service_id,
                    previous=previous_key,
                    launched=any(item.kind == "container" for item in created),
                    cleaned_up=cleanup.startswith("Removed the"),
                )
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
            cleanup = await self._teardown(request.target_id, plan, claim, created, target_os)
            if created and cleanup.startswith("Removed the"):
                forget_created(self.store, request.target_id, row.service_id, installed)
            if made_key:
                settle_failed_launch(
                    request.target_id,
                    row.service_id,
                    previous=previous_key,
                    launched=any(item.kind == "container" for item in created),
                    cleaned_up=cleanup.startswith("Removed the"),
                )
            self.store.put_operation(
                row.model_copy(
                    update={"state": "failed", "progress": f"Failed. {cleanup}", "error": str(exc)}
                )
            )

    async def _teardown(
        self,
        target_id: str,
        plan: DriverPlan | None,
        claim: ClaimResult | None,
        created: list[OwnedResource] | None = None,
        target_os: str = "linux",
    ) -> str:
        """Undo what a failed or cancelled plan started; report what happened.

        Resources the plan recorded creating (its ledger entries for this
        operation) are removed; a claim-based plan runs its own teardown, and
        only after the claim step: before it, this plan started nothing, and an
        adopted server was already running, so it is left alone.
        """

        if plan and plan.failure_cleanup:
            try:
                untouched = True
                for spec in plan.failure_cleanup:
                    result = await self._execute(target_id, spec)
                    if result.exit_code not in spec.allowed_exit_codes:
                        return (
                            f"Cleanup incomplete; retained data: {result.stderr or result.stdout}"
                        )
                    untouched = untouched and '"phase": "untouched"' in result.stdout
                if untouched:
                    return "Nothing this operation started needed cleaning up."
                return "Stopped the owned operation; retained its environment, model cache and evidence."
            except (OSError, RuntimeError) as exc:
                return f"Cleanup incomplete; retained data: {exc}"
        if created:

            async def execute(spec: CommandSpec) -> CommandResult:
                return await self._execute(target_id, spec)

            # A failed deploy keeps the shared image store (a retry resumes from it, F030).
            kept = [row for row in created if row.kind != "shared_image"]
            return (await remove_created(execute, target_id, kept, target_os))[1]
        if (
            plan is None
            or plan.teardown is None
            or claim is None
            or claim.result in {"adopted", "found"}
        ):
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

    _settle_service = settle_service

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
