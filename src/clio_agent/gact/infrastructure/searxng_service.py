"""SearXNG as a managed native service: CLIO's default, container-free web search backend.

SearXNG at a pinned commit runs in its OWN uv environment under the service directory
(never CLIO's environment), supervised by the shared native supervisor
(:mod:`~clio_agent.gact.infrastructure.node_service`): install, start, status, logs,
verify, stop, uninstall and delete_data, with the ownership marker, the reuse preflight
and failure cleanup every native service has. CLIO generates the settings (loopback
bind, limiter off for this private instance, JSON format on, no Valkey), the secret key
is written per installation to a 0600 file on the host, and engine API keys come from
CLIO's credential store at start through the process environment only.

Licensing: SearXNG is AGPL-3.0-or-later. CLIO downloads it unmodified at install time
and runs it as a separate process it talks to over HTTP; CLIO's own code (BSD-3-Clause)
is not combined with it. The only CLIO code that runs in SearXNG's interpreter is the
launcher and, on Windows, a ``pwd`` stand-in, both shipped as separate CLIO files.
"""

from __future__ import annotations

import hashlib
import json
import posixpath
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clio_agent.gact.infrastructure.models import (
    InfrastructureTarget,
    ManagedServiceDefinition,
    ServiceConfigurationField,
    ServiceVariant,
    TargetFacts,
)
from clio_agent.gact.infrastructure.plan import DriverPlan
from clio_agent.gact.infrastructure.storage import resolved_locations
from clio_agent.gact.infrastructure.supervised_service import supervised_plan
from clio_agent.search.settings import (
    API_KEY_ENGINES,
    SearchSettings,
    build_settings,
    engine_credential_ref,
    load_search_settings,
)

SERVICE_ID = "searxng"
VARIANT_ID = "native"
#: The SearXNG commit clio-web-search pins (its Dockerfile's SEARXNG_COMMIT).
SEARXNG_COMMIT = "e8e710e42a3ab2bce27d1f97e51d8d4ccaa80871"
SEARXNG_VERSION = "2026.8.11+e8e710e42"
SEARXNG_SOURCE_URL = f"https://github.com/searxng/searxng/archive/{SEARXNG_COMMIT}.tar.gz"
DEFINITION_VERSION = "searxng-native-1"
#: requirements.txt and requirements-server.txt at SEARXNG_COMMIT, exactly. Granian is
#: SearXNG's own pinned server; its ``[pname]`` extra (process renaming) is left out.
SEARXNG_REQUIREMENTS: tuple[str, ...] = (
    "certifi==2026.7.22",
    "babel==2.18.0",
    "flask-babel==4.0.0",
    "flask==3.1.3",
    "jinja2==3.1.6",
    "lxml==6.1.1",
    "pygments==2.20.0",
    "python-dateutil==2.9.0.post0",
    "pyyaml==6.0.3",
    "httpx[http2]==0.28.1",
    "httpx-socks[asyncio]==0.10.0",
    "sniffio==1.3.1",
    "valkey==6.1.1",
    "markdown-it-py==4.2.0",
    "msgspec==0.21.1",
    "typer==0.27.1",
    "isodate==0.7.2",
    "whitenoise==6.12.0",
    "typing-extensions==4.16.0",
    "granian==2.8.0",
)
#: One real query the ``verify`` action runs; at least one result with a URL passes.
VERIFY_QUERY = "HDF5 hierarchical data format"
#: CLIO files shipped into the service directory (published name -> source module).
SHIPPED_FILES = {
    "searxng_hook.py": "searxng_hook.py",
    "verify.py": "searxng_verify.py",
    "clio_pwd_shim.py": "searxng_pwd_shim.py",
}
HOOK = "searxng_hook.py"

EngineKeys = Callable[[str], str]


def _stored_engine_key(engine: str) -> str:
    from clio_agent.providers.api_key_store import ProviderApiKeyStore  # noqa: PLC0415

    return ProviderApiKeyStore().load(engine_credential_ref(engine))


def compatibility(facts: TargetFacts) -> tuple[bool, str]:
    """Whether the native variant can run on this host, and why (shown in the catalog)."""

    if facts.os == "windows":
        return False, (
            "Not available on Windows yet: CLIO's native service supervisor is POSIX-only. "
            "Use search.backend: clio_web_search. (Windows is not live-tested on this host.)"
        )
    if facts.os not in {"linux", "macos"} or facts.arch not in {"x86_64", "aarch64"}:
        return False, "Requires Linux or macOS on x86_64 or aarch64."
    if not facts.uv_available:
        return False, "Requires uv on this execution host to build SearXNG's own environment."
    if facts.os == "macos":
        return True, "Ready for native installation (macOS is not live-tested on this host)."
    return True, "Ready for native installation; no container runtime is needed."


def searxng_definition(facts: TargetFacts) -> ManagedServiceDefinition:
    """The catalog row of CLIO's private SearXNG."""

    compatible, reason = compatibility(facts)
    defaults = SearchSettings()
    return ManagedServiceDefinition(
        id=SERVICE_ID,
        category="scientific_service",
        label="SearXNG",
        description="CLIO's private web search: metasearch on this computer, no containers.",
        definition_version=DEFINITION_VERSION,
        recommended_variant=VARIANT_ID,
        variants=[
            ServiceVariant(
                id=VARIANT_ID,
                label="Native (uv)",
                version=SEARXNG_VERSION,
                install_type="native_uv",
                artifact=f"searxng@{SEARXNG_COMMIT}",
                compatible=compatible,
                reason=reason,
            )
        ],
        configuration_fields=[
            ServiceConfigurationField(id="port", label="Port", placeholder=str(defaults.port)),
            ServiceConfigurationField(
                id="engines",
                label="Engines (comma-separated)",
                placeholder=", ".join(defaults.engines),
            ),
            ServiceConfigurationField(
                id="opt_in_engines",
                label="Opt-in engines (required for baidu, sogou, 360search, ...)",
                placeholder="none",
            ),
            ServiceConfigurationField(
                id="safe_search",
                label="Safe search",
                placeholder=str(defaults.safe_search),
                options=["0", "1", "2"],
            ),
            ServiceConfigurationField(
                id="language", label="Search language", placeholder=defaults.language
            ),
            ServiceConfigurationField(
                id="request_timeout_s",
                label="Engine request timeout (seconds)",
                placeholder=str(defaults.request_timeout_s),
            ),
        ],
    )


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def effective_settings(
    configuration: dict[str, str], base: SearchSettings | None = None
) -> SearchSettings:
    """``search.*`` configuration, overridden by this deployment's form values."""

    defaults = base or load_search_settings()

    def number(key: str, fallback: float) -> float:
        raw = configuration.get(key, "").strip()
        try:
            return float(raw) if raw else fallback
        except ValueError as exc:
            raise ValueError(f"{key} must be a number") from exc

    engines = _csv(configuration.get("engines", ""))
    opt_in = _csv(configuration.get("opt_in_engines", ""))
    return build_settings(
        backend=defaults.backend,
        clio_web_search_url=defaults.clio_web_search_url,
        auto_install=defaults.auto_install,
        port=int(number("port", defaults.port)),
        engines=engines or list(defaults.engines),
        opt_in_engines=opt_in,
        safe_search=int(number("safe_search", defaults.safe_search)),
        request_timeout_s=number("request_timeout_s", defaults.request_timeout_s),
        max_results=defaults.max_results,
        language=configuration.get("language", "").strip() or defaults.language,
    )


def searxng_port(configuration: dict[str, str]) -> int:
    """The deployment's loopback port: its own ``port``, else ``search.searxng.port``."""

    raw = configuration.get("port", "").strip()
    if not raw:
        return load_search_settings().port
    if not raw.isdigit() or not 1024 <= int(raw) <= 65535:
        raise ValueError("SearXNG port must be between 1024 and 65535")
    return int(raw)


def searxng_settings(settings: SearchSettings, port: int) -> dict[str, Any]:
    """SearXNG's settings for CLIO's private instance; never carries a secret.

    The instance name (identity marker) is added on the host from the secret key.
    """

    engines = list(settings.engines)
    return {
        "use_default_settings": {"engines": {"keep_only": engines}},
        "general": {"debug": False, "instance_name": "CLIO SearXNG", "enable_metrics": False},
        "search": {
            "safe_search": settings.safe_search,
            "autocomplete": "",
            "default_lang": settings.language,
            "formats": ["html", "json"],
        },
        "server": {
            "port": port,
            "bind_address": "127.0.0.1",
            "limiter": False,
            "public_instance": False,
            "image_proxy": False,
            "method": "GET",
        },
        "valkey": {"url": False},
        "outgoing": {
            "request_timeout": settings.request_timeout_s,
            "max_request_timeout": min(60.0, settings.request_timeout_s * 2),
        },
        "engines": [{"name": name, "disabled": False} for name in engines],
    }


def engine_key_variable(engine: str) -> str:
    """The process-environment name one engine's API key travels under."""

    digest = hashlib.sha256(engine.encode()).hexdigest()[:8].upper()
    return f"CLIO_SEARXNG_ENGINE_KEY_{digest}"


def _shipped_files() -> dict[str, str]:
    here = Path(__file__).parent
    return {
        name: (here / source).read_text(encoding="utf-8") for name, source in SHIPPED_FILES.items()
    }


def searxng_plan(
    action: str,
    configuration: dict[str, str],
    facts: TargetFacts,
    target: InfrastructureTarget | None,
    *,
    base_settings: SearchSettings | None = None,
    engine_keys: EngineKeys = _stored_engine_key,
) -> DriverPlan:
    """Compile one lifecycle action of the native SearXNG deployment."""

    compatible, reason = compatibility(facts)
    if facts.os == "windows" or (action in {"install", "reinstall", "start"} and not compatible):
        raise ValueError(reason)
    target = target or InfrastructureTarget(id=facts.target_id, label=facts.label, kind="local")
    directory = configuration.get("storage.service_directory") or posixpath.join(
        resolved_locations(target, facts).service_data,
        facts.hostname or facts.target_id,
        SERVICE_ID,
    )
    ownership = hashlib.sha256(
        f"{facts.target_id}:{facts.hostname}:{directory}".encode()
    ).hexdigest()
    settings = effective_settings(configuration, base_settings)
    port = settings.port
    keyed = [name for name in settings.engines if name in API_KEY_ENGINES]
    secret_env: dict[str, str] = {}
    if action == "start":
        for engine in keyed:
            key = engine_keys(engine)
            if not key:
                raise ValueError(
                    f"Engine {engine!r} needs an API key: save it with "
                    f"PUT /v1/search/engine-keys/{engine}, or remove it from the engines"
                )
            secret_env[engine_key_variable(engine)] = key
    manifest: dict[str, Any] = {
        "service": SERVICE_ID,
        "definition_version": DEFINITION_VERSION,
        "project": (
            '[project]\nname="clio-searxng"\nversion="0.0.0"\nrequires-python=">=3.12,<3.13"\n'
            "dependencies=" + json.dumps(list(SEARXNG_REQUIREMENTS)) + "\n"
        ),
        "launcher": Path(__file__).with_name("searxng_launch.py").read_text(encoding="utf-8"),
        "files": _shipped_files(),
        "post_install": HOOK,
        "identity": {"kind": "components", "hook": HOOK},
        "port": port,
        "health_path": "/healthz",
        "arguments": [],
        "installation_bytes": 2 * 1024**3,
        "searxng": {
            "commit": SEARXNG_COMMIT,
            "version": SEARXNG_VERSION,
            "source_url": SEARXNG_SOURCE_URL,
            "settings": searxng_settings(settings, port),
            "verify_query": VERIFY_QUERY,
            "engine_keys": {name: engine_key_variable(name) for name in keyed},
            "engine_key_settings": {name: API_KEY_ENGINES[name] for name in keyed},
        },
    }
    resolved = {
        **configuration,
        "port": str(port),
        "engines": ",".join(settings.engines),
        "opt_in_engines": configuration.get("opt_in_engines", ""),
        "safe_search": str(settings.safe_search),
        "language": settings.language,
        "request_timeout_s": str(settings.request_timeout_s),
        "storage.service_directory": directory,
        "storage.captures": posixpath.join(directory, "evidence"),
        "compatibility_profile": DEFINITION_VERSION,
        "native_owner": ownership,
    }
    return supervised_plan(
        action,
        directory=directory,
        ownership=ownership,
        manifest=manifest,
        port=port,
        label="SearXNG",
        configuration=resolved,
        secret_env=secret_env,
    )
