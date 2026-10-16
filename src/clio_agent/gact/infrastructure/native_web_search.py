"""Container-free CLIO Web Search gateway with a private native SearXNG."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from clio_agent.gact.infrastructure.models import InfrastructureTarget, ServiceVariant, TargetFacts
from clio_agent.gact.infrastructure.plan import DriverPlan
from clio_agent.gact.infrastructure.searxng_service import (
    SEARXNG_COMMIT,
    SEARXNG_REQUIREMENTS,
    SEARXNG_SOURCE_URL,
    SEARXNG_VERSION,
    _stored_engine_key,
    compatibility,
    effective_settings,
    engine_key_variable,
    searxng_settings,
)
from clio_agent.gact.infrastructure.service_paths import service_directory
from clio_agent.gact.infrastructure.supervised_service import supervised_plan
from clio_agent.search.settings import API_KEY_ENGINES

REVISION = "306806e9c9a8ba11ec67aa3896bdf3d29a9768bd"
PROFILE = "web-search-native-1"


def native_web_variant(facts: TargetFacts) -> ServiceVariant:
    """Expose the gateway wherever the private native SearXNG dependencies are supported."""
    compatible, reason = compatibility(facts)
    if facts.os == "windows" and facts.target_id != "local":
        compatible, reason = False, "Native Windows installation runs beside the active CLIO."
    return ServiceVariant(
        id="native",
        label="Native gateway + SearXNG",
        version="0.3.2",
        install_type="native_uv",
        artifact=f"clio-web-search@{REVISION}",
        compatible=compatible,
        reason=reason,
    )


def native_web_plan(
    action: str,
    configuration: dict[str, str],
    facts: TargetFacts,
    target: InfrastructureTarget | None,
) -> DriverPlan:
    """Install, supervise and verify the real gateway without Docker/Podman/Apptainer."""
    selected = native_web_variant(facts)
    if action in {"install", "reinstall", "start"} and not selected.compatible:
        raise ValueError(selected.reason)
    port = int(configuration.get("port") or "8089")
    private = int(configuration.get("searxng_port") or "18888")
    if len({port, private}) != 2 or any(not 1024 <= value <= 65535 for value in (port, private)):
        raise ValueError("Gateway and private SearXNG require distinct ports from 1024 to 65535")
    documents = configuration.get("documents", "").startswith("on")
    grobid = configuration.get("grobid_url", "").strip()
    if documents and (
        urlsplit(grobid).scheme not in {"http", "https"} or not urlsplit(grobid).hostname
    ):
        raise ValueError("Native document conversion requires the URL of a running GROBID service")
    directory = configuration.get("storage.service_directory") or service_directory(
        "web_search", facts, target
    )
    owner = hashlib.sha256(f"{facts.target_id}:{facts.hostname}:{directory}".encode()).hexdigest()
    settings = effective_settings({**configuration, "port": str(private)})
    keyed = {
        name: engine_key_variable(name) for name in settings.engines if name in API_KEY_ENGINES
    }
    secret_env = {}
    if action == "start":
        for name, variable in keyed.items():
            key = _stored_engine_key(name)
            if not key:
                raise ValueError(
                    f"Search engine {name} requires its saved API key, or remove it from this service"
                )
            secret_env[variable] = key
    here = Path(__file__).parent
    names = (
        "native_web_search_hook.py",
        "searxng_hook.py",
        "searxng_pwd_shim.py",
        "searxng_launch.py",
        "searxng_verify.py",
    )
    files = {
        ("clio_pwd_shim.py" if name == "searxng_pwd_shim.py" else name): (here / name).read_text(
            encoding="utf-8"
        )
        for name in names
    }
    files["verify.py"] = files["native_web_search_hook.py"]
    extra = "[documents]" if documents else ""
    dependencies = [
        *SEARXNG_REQUIREMENTS,
        f"clio-web-search{extra} @ git+https://github.com/iowarp/clio-web-search.git@{REVISION}",
    ]
    manifest: dict[str, Any] = {
        "service": "web_search",
        "definition_version": PROFILE,
        "port": port,
        "health_path": "/readyz",
        "arguments": [],
        "installation_bytes": (8 if documents else 2) * 1024**3,
        "project": '[project]\nname="clio-native-web-search"\nversion="0.0.0"\nrequires-python=">=3.12,<3.13"\ndependencies='
        + json.dumps(dependencies)
        + "\n",
        "launcher": "from native_web_search_hook import main\nimport sys\nsys.argv=['launch','serve']\nmain()\n",
        "files": files,
        "post_install": "native_web_search_hook.py",
        "identity": {"kind": "components", "hook": "native_web_search_hook.py"},
        "gateway": {
            "documents": documents,
            "grobid_url": grobid,
            "contact_email": configuration.get("contact_email", ""),
        },
        "searxng": {
            "commit": SEARXNG_COMMIT,
            "version": SEARXNG_VERSION,
            "source_url": SEARXNG_SOURCE_URL,
            "settings": searxng_settings(settings, private),
            "verify_query": "HDF5 hierarchical data format",
            "engine_keys": keyed,
            "engine_key_settings": {name: API_KEY_ENGINES[name] for name in keyed},
        },
    }
    return supervised_plan(
        action,
        directory=directory,
        ownership=owner,
        manifest=manifest,
        port=port,
        label="CLIO Web Search",
        windows=facts.os == "windows",
        secret_env=secret_env,
        configuration={
            **configuration,
            "port": str(port),
            "searxng_port": str(private),
            "storage.service_directory": directory,
            "native_owner": owner,
            "compatibility_profile": PROFILE,
            "installation_method": "native",
        },
    )
