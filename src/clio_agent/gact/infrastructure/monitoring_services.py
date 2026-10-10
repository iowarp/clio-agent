"""Versioned, independent Flowcept and HPE CMF service definitions and drivers."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from pathlib import Path
from typing import Any

from clio_agent.gact.artifacts.provenance.cmf_document import (
    ArtifactEntry,
    ExecutionEntry,
    build_push_document,
)
from clio_agent.gact.infrastructure.models import (
    InfrastructureTarget,
    ManagedServiceDefinition,
    ServiceConfigurationField,
    ServiceVariant,
    TargetFacts,
)
from clio_agent.gact.infrastructure.native_vllm import FLOWCEPT_REVISION
from clio_agent.gact.infrastructure.plan import DriverPlan
from clio_agent.gact.infrastructure.storage import resolved_locations
from clio_agent.gact.infrastructure.supervised_service import supervised_plan

MONITORING_SERVICES = frozenset({"flowcept", "cmf"})
CMF_REVISION = "53d9c3e518ab2fde46955f10520d4842c572bf05"
CMF_BASE_IMAGE = "docker.io/library/python@sha256:f8aa74bffbe59d02f7442dba43c9ea72b2c34cc29aa7a7edc74b29521b23bb43"
MONGO_IMAGE = "docker.io/library/mongo@sha256:02a0cc7939f5ed38f30f9bc714ef5f682d49baf9350c54acf302ce833087fe8a"
REDIS_IMAGE = "docker.io/library/redis@sha256:02f2cc4882f8bf87c79a220ac958f58c700bdec0dfb9b9ea61b62fb0e8f1bfcf"
POSTGRES_IMAGE = "docker.io/library/postgres@sha256:4689940c683801b4ab839ab3b0a0a3555a5fe425371422310944e89eca7d8068"


def monitoring_port(service: str, configuration: dict[str, str]) -> int:
    """Validate the selected host's listener port."""
    port = configuration.get("port") or ("8008" if service == "flowcept" else "8380")
    if not port.isdigit() or not 1024 <= int(port) <= 65535:
        raise ValueError("Service port must be between 1024 and 65535")
    return int(port)


def monitoring_engines(service: str, facts: TargetFacts) -> list[str]:
    """Usable container runtimes this service's dependencies can run on."""
    # On Apptainer the CMF server runs from a pinned venv inside its base image's SIF.
    supported = {"docker", "podman", "apptainer"}
    engines = [row.name for row in facts.container_runtimes if row.usable and row.name in supported]
    if not engines and facts.docker_available:
        engines = ["docker"]
    return [str(engine) for engine in engines]


def cmf_host_server(port: int, postgres_port: int) -> dict[str, Any]:
    """The CMF server's host-network launch from its venv inside the base SIF."""
    return {
        "mounts": [
            ["data/cmf", "/cmf-server/data", False],
            ["source", "/cmf-server/src", True],
            ["cmf-venv", "/cmf-server/venv", True],
        ],
        "environment": {
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": str(postgres_port),
            "HOME": "/cmf-server/data",
            "CMF_LOG_FILE": "/cmf-server/data/cmflib.log",
        },
        "workdir": "/cmf-server/src",
        "host_arguments": [
            "/cmf-server/venv/bin/uvicorn",
            "server.app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        # Importing the app opens the metadata store and loads dvc/ray: allow minutes.
        "ready_seconds": 300,
        "host_check": [
            "/cmf-server/venv/bin/python",
            "-c",
            f"import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}/', timeout=5)",
        ],
    }


def monitoring_definitions(facts: TargetFacts) -> list[ManagedServiceDefinition]:
    """Render monitoring services without inference or each other as prerequisites."""
    result = []
    for service, label in (("flowcept", "Flowcept"), ("cmf", "HPE CMF")):
        engines = monitoring_engines(service, facts)
        compatible = facts.os == "linux" and facts.uv_available and bool(engines)
        runtimes = "Docker, Podman or Apptainer" if service == "flowcept" else "Docker or Podman"
        result.append(
            ManagedServiceDefinition(
                id=service,
                category="monitoring",
                label=label,
                description="Execution provenance and attention records."
                if service == "flowcept"
                else "Artifact lineage with direct server access.",
                definition_version=f"{service}-managed-2",
                recommended_variant="managed",
                variants=[
                    ServiceVariant(
                        id="managed",
                        label="Managed service",
                        version="1.0.3" if service == "flowcept" else CMF_REVISION[:8],
                        install_type="supervised_stack",
                        artifact=FLOWCEPT_REVISION if service == "flowcept" else CMF_REVISION,
                        compatible=compatible,
                        reason="Ready on this host."
                        if compatible
                        else f"Requires Linux, uv and usable {runtimes} on this execution host.",
                    )
                ],
                configuration_fields=[
                    ServiceConfigurationField(
                        id="container_runtime",
                        label="Dependency runtime",
                        options=engines,
                        placeholder=engines[0] if engines else "No supported engine",
                    ),
                    ServiceConfigurationField(
                        id="image_storage",
                        label="Container images",
                        options=["engine", "service"],
                        placeholder="service" if engines[:1] == ["apptainer"] else "engine",
                    ),
                    ServiceConfigurationField(
                        id="port",
                        label="Service port",
                        placeholder=str(monitoring_port(service, {})),
                    ),
                    *(
                        [
                            ServiceConfigurationField(
                                id="redis_port", label="Private Redis port", placeholder="16379"
                            ),
                            ServiceConfigurationField(
                                id="mongo_port", label="Private MongoDB port", placeholder="37017"
                            ),
                        ]
                        if service == "flowcept"
                        else [
                            ServiceConfigurationField(
                                id="server_image",
                                label="Existing server image digest (optional)",
                                placeholder="sha256:…; otherwise build the pinned HPE source",
                            ),
                        ]
                    ),
                ],
            )
        )
    return result


def verification_document() -> dict[str, Any]:
    """Use CLIO's canonical CMF mapping for a fresh input-to-output probe."""
    artifacts = {
        name: ArtifactEntry(
            artifact_id=name,
            name=f"{name}-CLIO_SETUP_PROBE:v1",
            uri=f"clio://setup/CLIO_SETUP_PROBE/{name}",
            cmf_type="Dataset",
            properties={
                "git_repo": "",
                "Commit": "",
                "url": f"clio://setup/CLIO_SETUP_PROBE/{name}",
            },
            custom_properties={"clio_setup_verification": "CLIO_SETUP_PROBE"},
            created_ms=0,
        )
        for name in ("input", "output")
    }
    return build_push_document(
        pipeline_name="clio-setup-verification",
        artifacts=artifacts,
        executions=[
            ExecutionEntry(
                call_id="CLIO_SETUP_PROBE",
                execution="verify_setup",
                custom_properties={},
                created_ms=0,
                used=["input"],
                generated=["output"],
            )
        ],
        created_ms=0,
    )


def monitoring_plan(
    service: str,
    action: str,
    configuration: dict[str, str],
    facts: TargetFacts,
    target: InfrastructureTarget,
) -> DriverPlan:
    """Compile the common supervisor and backend-specific dependencies and probes."""
    definition = next(row for row in monitoring_definitions(facts) if row.id == service)
    if action in {"install", "reinstall", "start"} and not definition.variants[0].compatible:
        raise ValueError(definition.variants[0].reason)
    locations = resolved_locations(target, facts)
    directory = configuration.get("storage.service_directory") or posixpath.join(
        locations.service_data, facts.hostname or facts.target_id, service
    )
    ownership = hashlib.sha256(
        f"{facts.target_id}:{facts.hostname}:{directory}".encode()
    ).hexdigest()
    prefix = f"clio-{service}-{ownership[:12]}"
    engine = (
        configuration.get("container_runtime") or definition.configuration_fields[0].placeholder
    )
    if engine not in {"docker", "podman"} and engine not in monitoring_engines(service, facts):
        raise ValueError(
            f"Choose a usable runtime for {definition.label} dependencies: "
            + ", ".join(monitoring_engines(service, facts) or ["none on this host"])
        )
    # Apptainer has no image store of its own: its SIFs always live in the service folder.
    image_storage = configuration.get("image_storage") or (
        "service" if engine == "apptainer" else "engine"
    )
    if image_storage not in {"engine", "service"}:
        raise ValueError("Choose engine storage or the service folder for container images")
    if image_storage == "service" and engine not in {"podman", "apptainer"}:
        raise ValueError(
            "Container images in the service folder require Podman or Apptainer on this host"
        )
    if engine == "apptainer" and image_storage != "service":
        raise ValueError("Apptainer keeps its images in the service folder")
    port = monitoring_port(service, configuration)
    files = {
        "stack.py": Path(__file__).with_name("node_service_stack.py").read_text(encoding="utf-8"),
        "verify.py": Path(__file__).with_name("monitoring_verify.py").read_text(encoding="utf-8"),
        "stack_apptainer.py": Path(__file__)
        .with_name("node_service_stack_apptainer.py")
        .read_text(encoding="utf-8"),
    }
    dependencies = ["httpx==0.28.1"]
    manifest: dict[str, Any] = {
        "service": service,
        "definition_version": definition.definition_version,
        "port": port,
        "health_path": "/api/v1/health/ready" if service == "flowcept" else "/",
        "container_runtime": engine,
        "image_storage": image_storage,
        "network": prefix,
        "installation_bytes": 1024**3,
        "files": files,
        "post_install": "stack.py",
        "hooks": dict.fromkeys(("stop", "uninstall", "delete_data", "logs"), "stack.py"),
        "identity": {"kind": "components", "hook": "stack.py"},
        "arguments": [],
        "launcher": Path(__file__).with_name("monitoring_launch.py").read_text(encoding="utf-8"),
    }
    if engine == "podman":
        manifest["pod_infra_image"] = (
            "registry.k8s.io/pause@sha256:ee6521f290b2168b6e0935a181d4cff9be1ac3f505666ef0e3c98fae8199917a"
        )
    if service == "flowcept":
        dependencies.append(
            f"flowcept[extras,webservice] @ git+https://github.com/spotter-ai-genesis/flowcept.git@{FLOWCEPT_REVISION}"
        )
        redis_port = monitoring_port(service, {"port": configuration.get("redis_port") or "16379"})
        mongo_port = monitoring_port(service, {"port": configuration.get("mongo_port") or "37017"})
        if len({port, redis_port, mongo_port}) != 3:
            raise ValueError("Flowcept, Redis and MongoDB require distinct ports")
        manifest.update(
            redis_port=redis_port,
            mongo_port=mongo_port,
            persistence_owner="managed_collector",
            settings={
                "project": {"db_flush_mode": "online"},
                "log": {
                    "log_path": posixpath.join(directory, "logs/flowcept.log"),
                    "log_file_level": "error",
                    "log_stream_level": "disable",
                },
                "mq": {
                    "enabled": True,
                    "type": "redis",
                    "host": "127.0.0.1",
                    "port": redis_port,
                    "channel": prefix,
                    "buffer_size": 1,
                    "insertion_buffer_time_secs": 0.5,
                },
                "kv_db": {"enabled": True, "host": "127.0.0.1", "port": redis_port},
                "databases": {
                    "mongodb": {
                        "enabled": True,
                        "host": "127.0.0.1",
                        "port": mongo_port,
                        "db": "clio",
                    },
                    "lmdb": {"enabled": False},
                },
                "db_buffer": {"buffer_size": 1, "insertion_buffer_time_secs": 0.5},
                "agent": {"enabled": False, "chat_enabled": False, "start_persistence": False},
                "web_server": {"dashboards_dir": posixpath.join(directory, "evidence/dashboards")},
            },
            components=[
                {
                    "name": prefix + "-redis",
                    "role": "redis",
                    "image": REDIS_IMAGE,
                    "ports": [[redis_port, 6379]],
                    "mounts": [
                        ["data/redis", "/data", False],
                        ["redis.conf", "/etc/redis.conf", True],
                    ],
                    # The private config is owned by the executing user. Rootless
                    # Podman maps that user to container root; bypass the image's
                    # privilege-dropping entrypoint rather than exposing the secret.
                    "entrypoint": "redis-server",
                    "arguments": ["/etc/redis.conf"],
                    "secrets": ["REDISCLI_AUTH"],
                    "check": ["sh", "-c", 'test "$(redis-cli --raw ping)" = PONG'],
                    "host_check": [
                        "sh",
                        "-c",
                        f'test "$(redis-cli -p {redis_port} --raw ping)" = PONG',
                    ],
                },
                {
                    "name": prefix + "-mongo",
                    "role": "mongo",
                    "image": MONGO_IMAGE,
                    "ports": [[mongo_port, 27017]],
                    "mounts": [["data/mongo", "/data/db", False]],
                    "secrets": ["MONGO_INITDB_ROOT_USERNAME", "MONGO_INITDB_ROOT_PASSWORD"],
                    # On the host network (Apptainer) mongod listens on loopback at its private port.
                    "host_arguments": [
                        "mongod",
                        "--bind_ip",
                        "127.0.0.1",
                        "--port",
                        str(mongo_port),
                    ],
                    "host_check": [
                        "mongosh",
                        "--quiet",
                        "--port",
                        str(mongo_port),
                        "--eval",
                        "quit(db.adminCommand({ping:1}).ok ? 0 : 1)",
                    ],
                    "check": [
                        "mongosh",
                        "--quiet",
                        "--eval",
                        "quit(db.adminCommand({ping:1}).ok ? 0 : 1)",
                    ],
                },
            ],
        )
    else:
        image = configuration.get("server_image", "").strip()
        if image and not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("An existing CMF server image must be its immutable sha256 image ID")
        if image and engine == "apptainer":
            raise ValueError("Apptainer runs the CMF server from its pinned source, not an image")
        if engine == "apptainer":
            # Apptainer cannot build the Dockerfile: the base image's SIF runs a
            # venv installed from a shipped, hash-pinned lock plus cmflib at the
            # pinned revision; server/app hardcodes /cmf-server/data, hence binds.
            image = CMF_BASE_IMAGE
            files["cmf-server.lock"] = (
                Path(__file__).with_name("cmf_server_py310.lock").read_text(encoding="utf-8")
            )
            manifest["installation_bytes"] = 3 * 1024**3
            manifest["source_environment"] = {
                "repository": "https://github.com/HewlettPackard/cmf.git",
                "revision": CMF_REVISION,
                "lock": "cmf-server.lock",
                # The only sdist allowed; pure Python and hash-pinned in the lock.
                "sdists": ["antlr4-python3-runtime"],
            }
        elif not image:
            image = f"clio-managed-cmf:{CMF_REVISION}"
            manifest["build"] = {
                "image": image,
                "repository": "https://github.com/HewlettPackard/cmf.git",
                "revision": CMF_REVISION,
                "dockerfile": "server/Dockerfile",
                "base_image": CMF_BASE_IMAGE,
                "upstream_base": "docker.io/library/python:3.10-slim-bullseye",
            }
        postgres_port = monitoring_port(
            service, {"port": configuration.get("postgres_port") or "15432"}
        )
        if postgres_port == port:
            raise ValueError("CMF and PostgreSQL require distinct ports")
        # On the host network (Apptainer) PostgreSQL listens on loopback at its private port.
        psql = f'psql -h 127.0.0.1 -p {postgres_port} -U "$POSTGRES_USER"'
        manifest.update(
            postgres_port=postgres_port,
            verification_document=verification_document(),
            components=[
                {
                    "name": prefix + "-postgres",
                    "role": "postgres",
                    "image": POSTGRES_IMAGE,
                    "mounts": [["data/postgres", "/var/lib/postgresql/data", False]],
                    "secrets": ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"],
                    "host_arguments": [
                        "postgres",
                        "-c",
                        "listen_addresses=127.0.0.1",
                        "-c",
                        f"port={postgres_port}",
                    ],
                    "host_check": [
                        "sh",
                        "-c",
                        f'PGPASSWORD="$POSTGRES_PASSWORD" {psql} -d postgres -tAc "SELECT 1"',
                    ],
                    "host_initialize": [
                        "sh",
                        "-c",
                        f'export PGPASSWORD="$POSTGRES_PASSWORD"; exists=$({psql} -d postgres -tAc '
                        f"\"SELECT 1 FROM pg_database WHERE datname='clio'\") || exit; "
                        f'[ "$exists" = 1 ] || createdb -h 127.0.0.1 -p {postgres_port} '
                        '-U "$POSTGRES_USER" clio',
                    ],
                    "check": [
                        "sh",
                        "-c",
                        'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d postgres -tAc "SELECT 1"',
                    ],
                    "initialize": [
                        "sh",
                        "-c",
                        'export PGPASSWORD="$POSTGRES_PASSWORD"; exists=$(psql -h 127.0.0.1 -U "$POSTGRES_USER" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname=\'clio\'") || exit; [ "$exists" = 1 ] || createdb -h 127.0.0.1 -U "$POSTGRES_USER" clio',
                    ],
                },
                {
                    "name": prefix + "-server",
                    "role": "server",
                    "image": image,
                    "ports": [[port, 8080]],
                    "mounts": [["data/cmf", "/cmf-server/data", False]],
                    "secrets": ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"],
                    "environment": {
                        "POSTGRES_HOST": "127.0.0.1" if engine == "podman" else "postgres",
                        "POSTGRES_PORT": "5432",
                        "HOME": "/cmf-server/data",
                        "CMF_LOG_FILE": "/cmf-server/data/cmflib.log",
                    },
                    **(cmf_host_server(port, postgres_port) if engine == "apptainer" else {}),
                },
            ],
        )
    manifest["project"] = (
        '[project]\nname="clio-managed-monitoring"\nversion="0.0.0"\nrequires-python=">=3.12,<3.13"\ndependencies='
        + json.dumps(dependencies)
        + "\n"
    )
    resolved = {
        **configuration,
        "container_runtime": engine,
        "image_storage": image_storage,
        "storage.container_images": posixpath.join(directory, "containers/images")
        if image_storage == "service"
        else "",
        "storage.container_downloads": posixpath.join(directory, "containers/downloads")
        if image_storage == "service"
        else "",
        "port": str(port),
        "storage.service_directory": directory,
        "storage.captures": posixpath.join(directory, "evidence"),
        "compatibility_profile": definition.definition_version,
        "native_owner": ownership,
    }
    if service == "flowcept":
        resolved.update(
            flowcept_settings=posixpath.join(directory, "settings.yaml"),
            persistence_owner="collector",
        )
    return supervised_plan(
        action,
        directory=directory,
        ownership=ownership,
        manifest=manifest,
        port=port,
        label=definition.label,
        configuration=resolved,
    )
