"""Allowlisted service definitions and lifecycle command plans owned by CLIO."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Iterable
from typing import cast
from uuid import uuid4

from clio_agent.gact.infrastructure.clio_agent_deploy import (
    CLIO_LAUNCHER,
    LAUNCHER_PRELUDE,
    claim_command,
    install_command,
    status_command,
    teardown_command,
)
from clio_agent.gact.infrastructure.context_sizing.deployment import sized_model_runtime_plan
from clio_agent.gact.infrastructure.controller_source import (
    controller_commit,
    deployment_version,
)
from clio_agent.gact.infrastructure.gpu_devices import LAUNCH_ACTIONS, check_gpu_devices
from clio_agent.gact.infrastructure.gpu_share import share_launch
from clio_agent.gact.infrastructure.model_instances import (
    ROUTER_SERVICE,
    engine_of,
    instance_label,
    instance_name,
    is_named_instance,
    validate_service_id,
)
from clio_agent.gact.infrastructure.model_router import (
    RouterInputs,
    router_definition,
    router_plan,
    router_port,
)
from clio_agent.gact.infrastructure.model_runtimes import (
    MODEL_RUNTIME_SERVICES,
    build_model_runtime_plan,
    model_runtime_definition,
    service_port,
)
from clio_agent.gact.infrastructure.models import (
    CommandSpec,
    InfrastructureTarget,
    ManagedServiceDefinition,
    OwnedResource,
    ServiceVariant,
    TargetFacts,
)
from clio_agent.gact.infrastructure.monitoring_services import (
    MONITORING_SERVICES,
    monitoring_definitions,
    monitoring_plan,
    monitoring_port,
)
from clio_agent.gact.infrastructure.plan import DriverPlan
from clio_agent.gact.infrastructure.remote_lifecycle import RemoteLaunch, start_owned_command
from clio_agent.gact.infrastructure.reuse import from_scratch
from clio_agent.gact.infrastructure.server_parameters import EngineId

__all__ = [
    "CLIO_AGENT_PORT",
    "LOOPBACK_ONLY_SERVICES",
    "DriverPlan",
    "build_driver_plan",
    "clio_agent_version",
    "deployed_clio_version",
    "service_connection_port",
    "service_definitions",
]

CLIO_AGENT_PORT = 17_800
# Services whose server listens on the target's loopback only: nothing can
# reach them at the host's address, so they are always reached through an SSH
# forward. CLIO's launcher binds 127.0.0.1, and managed model servers bind the
# loopback because they have no authentication (see model_runtimes). Named
# instances (``vllm@small``) are matched by their engine (:func:`loopback_only`).
LOOPBACK_ONLY_SERVICES = frozenset(
    {
        "clio_agent",
        ROUTER_SERVICE,
        *MODEL_RUNTIME_SERVICES,
        *MONITORING_SERVICES,
    }
)


def loopback_only(service_id: str) -> bool:
    """Whether ``service_id`` (or the engine of a named instance) listens on loopback only."""

    return engine_of(service_id) in LOOPBACK_ONLY_SERVICES


def clio_agent_version() -> str:
    """The clio-agent version this CLIO runs: the one a remote CLIO installs."""

    return importlib.metadata.version("clio-agent")


def deployed_clio_version() -> str:
    """The version a remote CLIO must report to run this CLIO's code (``+g<commit>`` from source)."""

    return deployment_version(clio_agent_version())


def _clio_agent_artifact() -> str:
    commit = controller_commit()
    if commit:
        return f"clio-agent at commit {commit[:12]} (source)"
    return f"clio-agent=={clio_agent_version()} (PyPI)"


def service_connection_port(
    service_id: str, configuration: dict[str, str] | None = None, variant_id: str = ""
) -> int | None:
    """Return the listener port for a connectable managed service.

    Containerized model runtimes listen on a configurable port
    (``configuration["port"]``); the native Windows llama.cpp on its fixed one.
    """

    if engine_of(service_id) in MODEL_RUNTIME_SERVICES:
        return service_port(service_id, configuration or {}, variant_id)
    if service_id == ROUTER_SERVICE:
        return router_port(configuration or {})
    if service_id in MONITORING_SERVICES:
        return monitoring_port(service_id, configuration or {})
    if service_id == "clio_agent":
        value = (configuration or {}).get("port", str(CLIO_AGENT_PORT))
        if not value.isdigit() or not 1024 <= int(value) <= 65535:
            raise ValueError("Remote CLIO port must be between 1024 and 65535")
        return int(value)
    return None


def service_definitions(
    facts: TargetFacts, instances: Iterable[str] = ()
) -> list[ManagedServiceDefinition]:
    """Build service compatibility from inspected host facts.

    ``instances`` are the named model-runtime instances (``vllm@small``) to
    list beside their engine's default deployment.
    """

    clio_agent = ManagedServiceDefinition(
        id="clio_agent",
        category="remote_access",
        label="CLIO",
        description="Install and operate a CLIO service on this SSH host.",
        recommended_variant="released",
        variants=[
            # Exactly this CLIO's version, so the desktop and remote match.
            ServiceVariant(
                id="released",
                label="Published release",
                version=deployed_clio_version(),
                install_type="bootstrap",
                artifact=_clio_agent_artifact(),
                compatible=facts.os == "linux" and facts.target_id != "local",
                reason="Remote CLIO deployment requires a Linux SSH target.",
            )
        ],
    )
    named: dict[str, list[ManagedServiceDefinition]] = {}
    for service_id in dict.fromkeys(instances):
        if is_named_instance(service_id) and engine_of(service_id) in MODEL_RUNTIME_SERVICES:
            engine = model_runtime_definition(engine_of(service_id), facts)
            named.setdefault(engine.id, []).append(
                engine.model_copy(
                    update={
                        "id": service_id,
                        "label": instance_label(engine.label, service_id),
                        "supports_instances": False,
                        "instance_of": engine.id,
                        "instance_name": instance_name(service_id),
                    }
                )
            )
    return [
        model_runtime_definition("vllm", facts),
        *named.get("vllm", []),
        router_definition(facts),
        model_runtime_definition("llama_cpp", facts),
        *named.get("llama_cpp", []),
        model_runtime_definition("ollama", facts),
        *named.get("ollama", []),
        *monitoring_definitions(facts),
        clio_agent,
    ]


def build_driver_plan(
    *,
    service_id: str,
    action: str,
    variant_id: str,
    configuration: dict[str, str],
    facts: TargetFacts,
    target: InfrastructureTarget | None = None,
    owned: list[OwnedResource] | None = None,
    api_key: str | None = None,
    on_conflict: str | None = None,
    resolved_root: str | None = None,
    router: RouterInputs | None = None,
) -> DriverPlan:
    """Compile one allowlisted lifecycle action into commands.

    ``owned`` is the service's ledger of what its deployment created; model
    runtimes remove exactly those resources on uninstall and reinstall.
    ``api_key`` is a keyed model server's deployment key (see
    :mod:`clio_agent.gact.infrastructure.server_access`). ``on_conflict``
    (``clio_agent`` only) is this ONE operation's answer to a found conflict
    -- the caller must never read it back out of a persisted record and pass
    it again; a fresh claim asks again every time. ``resolved_root``
    overrides ``target.install_root`` when a prior ``connect`` adopted the
    service under a different root than the target's configured one.
    ``router`` is what a model-router start routes to (its instances and keys).
    """

    validate_service_id(service_id)
    definitions = {row.id: row for row in service_definitions(facts, [service_id])}
    definition = definitions.get(service_id)
    if definition is None:
        raise ValueError(f"Unknown managed service {service_id!r}")
    engine = engine_of(service_id)
    if action == "delete_data" and not (
        (engine == "vllm" and variant_id.startswith(("native-cuda", "native-xpu")))
        or service_id in MONITORING_SERVICES
        or service_id == ROUTER_SERVICE
    ):
        raise ValueError("This service does not support separate deletion of retained data")
    variant = next((row for row in definition.variants if row.id == variant_id), None)
    if variant is None:
        raise ValueError(f"Unknown {service_id} variant {variant_id!r}")
    if action == "verify" and service_id not in MONITORING_SERVICES:
        raise ValueError("This service definition has no setup verification procedure")
    if service_id in MONITORING_SERVICES:
        return monitoring_plan(
            service_id,
            action,
            configuration,
            facts,
            target or InfrastructureTarget(id=facts.target_id, label=facts.label, kind="local"),
            variant_id,
        )
    if service_id == ROUTER_SERVICE:
        return router_plan(action, configuration, facts, target, api_key, router)
    if engine in MODEL_RUNTIME_SERVICES:
        # The model-runtime driver checks its own compatibility, so a missing
        # container runtime surfaces as the typed RuntimeUnavailableError. The
        # context is sized around it (context_sizing.deployment), and a vLLM
        # GPU share is its memory utilization (gpu_share). A GPU pin must name
        # this host's GPUs (gpu_devices), checked against the facts just probed.
        if action in LAUNCH_ACTIONS:
            check_gpu_devices(engine, configuration, facts.gpus)

        def model_plan(sized: dict[str, str]) -> DriverPlan:
            return build_model_runtime_plan(
                service_id=service_id,
                action=action,
                variant_id=variant_id,
                configuration=sized,
                facts=facts,
                target=target,
                owned=owned,
                api_key=api_key,
            )

        return sized_model_runtime_plan(
            engine=cast(EngineId, engine),
            action=action,
            variant_id=variant_id,
            configuration=configuration,
            facts=facts,
            build=share_launch(model_plan, engine, variant_id),
        )
    if action in {"install", "reinstall"} and not variant.compatible:
        raise ValueError(variant.reason or "This service is unavailable on the selected target")

    if service_id == "clio_agent":
        return _clio_agent_plan(
            action,
            target,
            on_conflict=on_conflict,
            resolved_root=resolved_root,
            configuration=configuration,
        )

    raise ValueError(f"Unknown managed service {service_id!r}")


def _clio_agent_plan(
    action: str,
    target: InfrastructureTarget | None,
    *,
    on_conflict: str | None = None,
    resolved_root: str | None = None,
    configuration: dict[str, str] | None = None,
) -> DriverPlan:
    if target is None or target.kind != "ssh":
        raise ValueError("Remote CLIO deployment requires an SSH infrastructure target")
    # A prior `connect` may have adopted this service under a root that
    # differs from the target's configured one; lifecycle commands must act
    # on where the process actually lives, not where a fresh install would go.
    root = (resolved_root or target.install_root).strip()
    configuration = configuration or {}
    port = service_connection_port("clio_agent", configuration)
    assert port is not None
    if on_conflict not in {None, "connect", "replace", "update"}:
        raise ValueError("Choose reconnect, update, or replace for the existing CLIO")
    if on_conflict == "replace":
        root = target.install_root.strip()
    # "Start it and connect" for an agent installed but stopped starts that
    # install as it is, at its own version: no reinstall.
    start_existing = on_conflict == "connect" and configuration.get("conflict_state") == "stopped"
    if on_conflict == "update" or start_existing:
        root = configuration.get("conflict_root", "").strip()
        if not root.startswith("/"):
            raise ValueError("Using the existing agent requires its absolute installation path")

    def launcher(script: str) -> CommandSpec:
        return CommandSpec(
            program="bash",
            args=[
                "-lc",
                LAUNCHER_PRELUDE + 'export CLIO_PORT="$2"; ' + CLIO_LAUNCHER + script,
                "clio",
                root,
                str(port),
            ],
        )

    if action == "status":
        return DriverPlan((status_command(root, port),), connection_port=port)
    if action == "logs":
        return DriverPlan((launcher("clio_launcher logs"),), connection_port=port)
    if action == "stop":
        return DriverPlan((launcher("clio_launcher stop"),))
    if action == "uninstall":
        return DriverPlan(
            (
                launcher(
                    'marker="$root/.clio-managed-install"; '
                    'if [ ! -f "$marker" ]; then '
                    "echo 'Refusing to remove an unmarked install root' >&2; exit 73; fi; "
                    'clio_launcher stop || true; rm -rf -- "$root"'
                ),
            )
        )
    if action not in {"install", "reinstall", "start"}:
        raise ValueError(f"Unsupported CLIO lifecycle action {action!r}")
    version = clio_agent_version()
    # Claim the port FIRST, before this root's own state is touched: a
    # reinstall's uninstall step (stop + rm -rf) must never run ahead of the
    # conflict check, or a found conflict destroys this root's install for
    # nothing while leaving the actual conflict on the port untouched (#1528
    # review). Adopt this install's healthy server of this version, or stop
    # any other CLIO on the port, only once claimed; never start beside one
    # -- unless the person already chose "Replace it" for a found conflict
    # (`on_conflict: "replace"`, an operation-scoped answer runtime.py never
    # persists), which is the only case that may stop a CLIO this claim
    # doesn't recognize as its own.
    replace = on_conflict in {"replace", "update"}
    commands: list[CommandSpec] = [
        claim_command(
            root,
            port,
            version,
            replace=replace,
            expected_pid=configuration.get("conflict_pid", ""),
            answered=on_conflict is not None,
        )
    ]
    if action == "reinstall":
        commands.extend(
            _clio_agent_plan(
                "uninstall", target, resolved_root=resolved_root, configuration=configuration
            ).commands
        )
    if action in {"install", "reinstall"} and not start_existing:
        commands.append(
            install_command(
                root, version, fresh=from_scratch(configuration), source_commit=controller_commit()
            )
        )
    launch = RemoteLaunch(
        root,
        port,
        str(uuid4()),
        configuration.get("keep_running") == "true",
        configuration.get("desktop_id", ""),
        target.label,
    )
    commands.append(start_owned_command(launch))
    return DriverPlan(
        tuple(commands),
        connection_port=port,
        remote_launch=launch,
        teardown=lambda claim: teardown_command(
            root, port, purge_root=not claim.existing_root, launch_token=launch.token
        ),
    )
