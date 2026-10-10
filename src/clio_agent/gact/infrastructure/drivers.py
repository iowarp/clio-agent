"""Allowlisted service definitions and lifecycle command plans owned by CLIO."""

from __future__ import annotations

import importlib.metadata
from collections.abc import Iterable
from typing import cast
from uuid import uuid4

from clio_agent.gact.infrastructure.clio_agent_deploy import (
    LAUNCHER_PRELUDE,
    claim_command,
    install_command,
    status_command,
    teardown_command,
)
from clio_agent.gact.infrastructure.context_sizing.deployment import sized_model_runtime_plan
from clio_agent.gact.infrastructure.gpu_share import share_launch
from clio_agent.gact.infrastructure.model_instances import (
    ROUTER_SERVICE,
    engine_of,
    instance_label,
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
    CommandResult,
    CommandSpec,
    InfrastructureTarget,
    ManagedServiceDefinition,
    OwnedResource,
    ServiceConfigurationField,
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
from clio_agent.gact.infrastructure.reuse import Reuse, ReuseCheck, from_scratch
from clio_agent.gact.infrastructure.searxng_service import (
    SERVICE_ID as SEARXNG_SERVICE,
)
from clio_agent.gact.infrastructure.searxng_service import (
    searxng_definition,
    searxng_plan,
    searxng_port,
)
from clio_agent.gact.infrastructure.server_parameters import EngineId
from clio_agent.gact.infrastructure.web_search_service import (
    WEB_SEARCH_IMAGE,
    build_web_search_plan,
    web_search_definition,
    web_search_port,
)

__all__ = [
    "CLIO_AGENT_PORT",
    "WEB_SEARCH_IMAGE",
    "LOOPBACK_ONLY_SERVICES",
    "DriverPlan",
    "build_driver_plan",
    "clio_agent_version",
    "service_connection_port",
    "service_definitions",
]

RELAY_VERSION = "1.6.8"
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
        SEARXNG_SERVICE,
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
    if service_id == "web_search":
        return web_search_port(configuration)
    if service_id == SEARXNG_SERVICE:
        return searxng_port(configuration or {})
    return None


def _field(
    field_id: str,
    label: str,
    placeholder: str,
    *,
    required: bool = False,
    options: list[str] | None = None,
) -> ServiceConfigurationField:
    return ServiceConfigurationField(
        id=field_id,
        label=label,
        placeholder=placeholder,
        required=required,
        options=options or [],
    )


def service_definitions(
    facts: TargetFacts, instances: Iterable[str] = ()
) -> list[ManagedServiceDefinition]:
    """Build service compatibility from inspected host facts.

    ``instances`` are the named model-runtime instances (``vllm@small``) to
    list beside their engine's default deployment.
    """

    relay = ManagedServiceDefinition(
        id="relay",
        category="remote_access",
        label="CLIO Relay",
        description="Deploy and inspect persistent work on an SSH cluster.",
        recommended_variant="uv-tool",
        variants=[
            ServiceVariant(
                id="uv-tool",
                label="Released uv tool",
                version=RELAY_VERSION,
                install_type="uv_tool",
                artifact=f"clio-relay=={RELAY_VERSION}",
                compatible=facts.uv_available,
                reason="Requires uv on the managing CLIO host.",
            )
        ],
        configuration_fields=[
            _field("cluster_name", "Cluster name", "my-cluster", required=True),
            _field("agent_bin", "Remote agent executable", "agent", required=True),
            _field(
                "relay_artifact_sha256", "Relay wheel SHA-256", "Published SHA-256", required=True
            ),
        ],
        supports_stop=False,
    )
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
                version=clio_agent_version(),
                install_type="bootstrap",
                artifact=f"clio-agent=={clio_agent_version()} (PyPI)",
                compatible=facts.os == "linux" and facts.target_id != "local",
                reason="Remote CLIO deployment requires a Linux SSH target.",
            )
        ],
    )
    named: list[ManagedServiceDefinition] = []
    for service_id in dict.fromkeys(instances):
        if is_named_instance(service_id) and engine_of(service_id) in MODEL_RUNTIME_SERVICES:
            engine = model_runtime_definition(engine_of(service_id), facts)
            label = instance_label(engine.label, service_id)
            named.append(engine.model_copy(update={"id": service_id, "label": label}))
    return [
        model_runtime_definition("vllm", facts),
        *named,
        router_definition(facts),
        model_runtime_definition("llama_cpp", facts),
        model_runtime_definition("ollama", facts),
        *monitoring_definitions(facts),
        web_search_definition(facts),
        searxng_definition(facts),
        relay,
        clio_agent,
    ]


def _required(configuration: dict[str, str], key: str) -> str:
    value = configuration.get(key, "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    if "\0" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{key} contains invalid control characters")
    return value


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
        (engine == "vllm" and variant_id.startswith("native-cuda"))
        or service_id in MONITORING_SERVICES
        or service_id in {"web_search", SEARXNG_SERVICE}
        or service_id == ROUTER_SERVICE
    ):
        raise ValueError("This service does not support separate deletion of retained data")
    variant = next((row for row in definition.variants if row.id == variant_id), None)
    if variant is None:
        raise ValueError(f"Unknown {service_id} variant {variant_id!r}")
    if action == "verify" and service_id not in {
        *MONITORING_SERVICES,
        "web_search",
        SEARXNG_SERVICE,
    }:
        raise ValueError("This service definition has no setup verification procedure")
    if service_id == SEARXNG_SERVICE:
        return searxng_plan(action, configuration, facts, target)
    if service_id in MONITORING_SERVICES:
        return monitoring_plan(
            service_id,
            action,
            configuration,
            facts,
            target or InfrastructureTarget(id=facts.target_id, label=facts.label, kind="local"),
        )
    if service_id == ROUTER_SERVICE:
        return router_plan(action, configuration, facts, target, api_key, router)
    if engine in MODEL_RUNTIME_SERVICES:
        # The model-runtime driver checks its own compatibility, so a missing
        # container runtime surfaces as the typed RuntimeUnavailableError. The
        # context is sized around it (context_sizing.deployment), and a vLLM
        # GPU share is its memory utilization (gpu_share).
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

    if service_id == "relay":
        return _relay_plan(action, configuration, target)
    if service_id == "clio_agent":
        return _clio_agent_plan(
            action,
            target,
            on_conflict=on_conflict,
            resolved_root=resolved_root,
            configuration=configuration,
        )

    return build_web_search_plan(action, configuration, facts, target, owned)


def _relay_plan(
    action: str,
    configuration: dict[str, str],
    target: InfrastructureTarget | None,
) -> DriverPlan:
    cluster = _required(configuration, "cluster_name")
    if action in {"status", "logs"}:
        return DriverPlan(
            (
                CommandSpec(
                    program="clio-relay",
                    args=["cluster", "endpoint-service-status", "--cluster", cluster],
                    scope="controller",
                ),
            )
        )
    if action == "stop":
        raise ValueError("Relay workers are persistent; use Relay teardown for destructive cleanup")
    if action == "uninstall":
        return DriverPlan(
            (
                CommandSpec(
                    program="uv",
                    args=["tool", "uninstall", "clio-relay"],
                    scope="controller",
                    allowed_exit_codes=[0, 1],
                ),
            )
        )
    agent = _required(configuration, "agent_bin")
    sha = _required(configuration, "relay_artifact_sha256")
    if len(sha) != 64 or any(character not in "0123456789abcdefABCDEF" for character in sha):
        raise ValueError("relay_artifact_sha256 must contain exactly 64 hexadecimal characters")
    commands = [
        CommandSpec(
            program="uv",
            args=[
                "tool",
                "install",
                "--python",
                "3.13",
                "--no-config",
                *(["--reinstall"] if from_scratch(configuration) else []),
                f"clio-relay=={RELAY_VERSION}",
            ],
            scope="controller",
            timeout_seconds=900,
        ),
        CommandSpec(
            program="clio-relay",
            args=[
                "cluster",
                "add",
                "--name",
                cluster,
                "--ssh-host",
                _ssh_destination(target),
                "--scheduler-provider",
                "slurm",
                "--agent-adapter",
                "exec",
                "--agent-bin",
                agent,
            ],
            scope="controller",
        ),
        CommandSpec(
            program="clio-relay",
            args=["cluster", "bootstrap", "--cluster", cluster, "--relay-artifact-sha256", sha],
            scope="controller",
            timeout_seconds=900,
        ),
        CommandSpec(
            program="clio-relay",
            args=[
                "cluster",
                "install-endpoint-service",
                "--cluster",
                cluster,
                "--start",
                "--enable",
            ],
            scope="controller",
            timeout_seconds=900,
        ),
    ]
    return DriverPlan(tuple(commands), reuse_checks={0: ReuseCheck((), _relay_reused)})


def _relay_reused(result: CommandResult) -> Reuse | None:
    """uv's own verdict that this exact Relay version is already installed."""

    if "is already installed" not in result.stdout + result.stderr:
        return None
    return Reuse(kind="package", thing="Relay", identity=f"clio-relay=={RELAY_VERSION}")


def _ssh_destination(target: InfrastructureTarget | None) -> str:
    if target is None or target.kind != "ssh" or target.ssh is None:
        raise ValueError("Relay installation requires an SSH infrastructure target")
    if target.ssh.profile.strip():
        return target.ssh.profile.strip()
    host = target.ssh.host.strip()
    if not host:
        raise ValueError("Relay target requires an SSH host")
    return f"{target.ssh.user.strip()}@{host}" if target.ssh.user.strip() else host


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
    if on_conflict == "update":
        root = configuration.get("conflict_root", "").strip()
        if not root.startswith("/"):
            raise ValueError("Updating requires the existing agent's absolute installation path")

    def launcher(script: str) -> CommandSpec:
        return CommandSpec(
            program="bash",
            args=[
                "-lc",
                LAUNCHER_PRELUDE + 'export CLIO_PORT="$2"; ' + script,
                "clio",
                root,
                str(port),
            ],
        )

    if action == "status":
        return DriverPlan((status_command(root, port),), connection_port=port)
    if action == "logs":
        return DriverPlan((launcher('"$bin/clio" logs'),), connection_port=port)
    if action == "stop":
        return DriverPlan((launcher('"$bin/clio" stop'),))
    if action == "uninstall":
        return DriverPlan(
            (
                launcher(
                    'marker="$root/.clio-managed-install"; '
                    'if [ ! -f "$marker" ]; then '
                    "echo 'Refusing to remove an unmarked install root' >&2; exit 73; fi; "
                    '"$bin/clio" stop || true; rm -rf -- "$root"'
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
            root, port, version, replace=replace, expected_pid=configuration.get("conflict_pid", "")
        )
    ]
    if action == "reinstall":
        commands.extend(
            _clio_agent_plan(
                "uninstall", target, resolved_root=resolved_root, configuration=configuration
            ).commands
        )
    if action in {"install", "reinstall"}:
        commands.append(install_command(root, version, fresh=from_scratch(configuration)))
    launch = RemoteLaunch(
        root,
        port,
        str(uuid4()),
        configuration.get("keep_running") == "true",
        configuration.get("desktop_id", ""),
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
