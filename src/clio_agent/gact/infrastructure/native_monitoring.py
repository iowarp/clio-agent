"""Container-free Flowcept and CMF with managed or site-provided databases."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from clio_agent.gact.infrastructure.models import InfrastructureTarget, ServiceVariant, TargetFacts
from clio_agent.gact.infrastructure.native_vllm import FLOWCEPT_REVISION
from clio_agent.gact.infrastructure.plan import DriverPlan
from clio_agent.gact.infrastructure.service_paths import service_directory
from clio_agent.gact.infrastructure.supervised_service import supervised_plan

CMF_REVISION = "53d9c3e518ab2fde46955f10520d4842c572bf05"
PROFILE = "monitoring-native-1"


def native_monitoring_variant(service: str, facts: TargetFacts) -> ServiceVariant:
    """Describe the upstream Linux Python/database footprint without requiring containers."""
    reason = (
        "Native managed monitoring currently requires Linux x86-64."
        if facts.os != "linux" or facts.arch != "x86_64"
        else "Install uv on this execution host, then inspect again."
        if not facts.uv_available
        else ""
    )
    return ServiceVariant(
        id="native",
        label="Native application + native/site databases",
        version=FLOWCEPT_REVISION[:8] if service == "flowcept" else CMF_REVISION[:8],
        install_type="native_uv",
        artifact=f"{service}@{PROFILE}",
        compatible=not reason,
        reason=reason or "No container runtime is needed.",
    )


def native_monitoring_plan(
    service: str,
    action: str,
    configuration: dict[str, str],
    facts: TargetFacts,
    target: InfrastructureTarget,
    verification_document: dict[str, Any],
) -> DriverPlan:
    """Prepare one independently owned native stack, retaining its database files on uninstall."""
    selected = native_monitoring_variant(service, facts)
    if action in {"install", "reinstall", "start"} and not selected.compatible:
        raise ValueError(selected.reason)
    mode = configuration.get("database_mode") or "managed"
    if mode not in {"managed", "site"}:
        raise ValueError("Choose managed native databases or site-provided databases")
    settings_path = configuration.get("database_settings_path", "").strip()
    if mode == "site" and not settings_path.startswith("/"):
        raise ValueError(
            "Site databases require an absolute private database-settings file on this host"
        )
    port = int(configuration.get("port") or ("8008" if service == "flowcept" else "8380"))
    redis = int(configuration.get("redis_port") or "16379")
    mongo = int(configuration.get("mongo_port") or "37017")
    postgres = int(configuration.get("postgres_port") or "15432")
    ports = [port, redis, mongo] if service == "flowcept" else [port, postgres]
    if len(set(ports)) != len(ports) or any(not 1024 <= value <= 65535 for value in ports):
        raise ValueError(
            "Native application and database listeners need distinct ports from 1024 to 65535"
        )
    directory = configuration.get("storage.service_directory") or service_directory(
        service, facts, target
    )
    owner = hashlib.sha256(f"{facts.target_id}:{facts.hostname}:{directory}".encode()).hexdigest()
    dependencies = [
        "httpx==0.28.1",
        *(
            []
            if service == "cmf"
            else [
                f"flowcept[extras,webservice] @ git+https://github.com/spotter-ai-genesis/flowcept.git@{FLOWCEPT_REVISION}"
            ]
        ),
    ]
    python = "3.10" if service == "cmf" else "3.12"
    here = Path(__file__).parent
    files = {
        name: (here / name).read_text(encoding="utf-8")
        for name in ("native_monitoring_hook.py", "native_archive.py")
    }
    files["stack.py"] = "from native_monitoring_hook import flowcept_environment\n"
    files["verify.py"] = (here / "monitoring_verify.py").read_text(encoding="utf-8")
    if service == "cmf":
        files["cmf-server.lock"] = (here / "cmf_server_py310.lock").read_text(encoding="utf-8")
    manifest: dict[str, Any] = {
        "service": service,
        "definition_version": PROFILE,
        "port": port,
        "health_path": "/api/v1/health/ready" if service == "flowcept" else "/",
        "python_version": python,
        "arguments": [],
        "installation_bytes": 6 * 1024**3,
        "project": '[project]\nname="clio-native-monitoring"\nversion="0.0.0"\nrequires-python=">='
        + python
        + ",<"
        + ("3.11" if service == "cmf" else "3.13")
        + '"\ndependencies='
        + json.dumps(dependencies)
        + "\n",
        "files": files,
        "post_install": "native_monitoring_hook.py",
        "launcher": "from native_monitoring_hook import main\nimport sys\nsys.argv=['launch','serve']\nmain()\n",
        "identity": {"kind": "components", "hook": "native_monitoring_hook.py"},
        "database_mode": mode,
        "database_settings_path": settings_path,
        "credential_fields": ["password", "redis_url", "mongodb_uri", "POSTGRES_PASSWORD"],
        "redis_port": redis,
        "mongo_port": mongo,
        "postgres_port": postgres,
        "cmf_revision": CMF_REVISION,
        "verification_document": verification_document,
    }
    return supervised_plan(
        action,
        directory=directory,
        ownership=owner,
        manifest=manifest,
        port=port,
        label="Flowcept" if service == "flowcept" else "HPE CMF",
        configuration={
            **configuration,
            "database_mode": mode,
            "storage.service_directory": directory,
            "storage.captures": directory + "/evidence",
            **(
                {
                    "flowcept_settings": directory + "/settings.yaml",
                    "persistence_owner": "collector",
                }
                if service == "flowcept"
                else {}
            ),
            "native_owner": owner,
            "compatibility_profile": PROFILE,
            "installation_method": "native",
            "port": str(port),
        },
    )
