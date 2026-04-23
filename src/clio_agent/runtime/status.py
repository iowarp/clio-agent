"""Runtime integration status probes for CLIO Agent.

The doctor path should report what is actually configured and reachable without
booting a full agent or requiring live IOWarp/clio-core services.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

import requests

from clio_agent.config import PROVIDER_DEFAULTS, LMProviderConfig
from clio_agent.tools.file_policy import FileAccessPolicy, FilePolicyError


class IntegrationState(str, Enum):
    """Explicit runtime status values for integration checks."""

    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    MISCONFIGURED = "misconfigured"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class IntegrationStatus:
    """Status for one configured runtime integration."""

    name: str
    state: IntegrationState
    summary: str
    config_source: str
    next_action: str
    endpoint: str | None = None
    auth_mode: str | None = None
    fallback: str | None = None
    capabilities: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable status dictionary."""
        return {
            "name": self.name,
            "status": self.state.value,
            "summary": self.summary,
            "config_source": self.config_source,
            "next_action": self.next_action,
            "endpoint": self.endpoint,
            "auth_mode": self.auth_mode,
            "fallback": self.fallback,
            "capabilities": self.capabilities,
            "details": self.details,
            "required": self.required,
        }


@dataclass(frozen=True)
class RuntimeReport:
    """Complete runtime integration report."""

    integrations: list[IntegrationStatus]
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )

    @property
    def overall_status(self) -> str:
        """Return a compact aggregate status for required integrations."""
        required = [item for item in self.integrations if item.required]
        active_required = [item for item in required if item.state != IntegrationState.SKIPPED]
        if not active_required:
            return IntegrationState.SKIPPED.value
        if any(
            item.state
            in {
                IntegrationState.DEGRADED,
                IntegrationState.UNAVAILABLE,
                IntegrationState.MISCONFIGURED,
            }
            for item in active_required
        ):
            return IntegrationState.DEGRADED.value
        return IntegrationState.READY.value

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable report dictionary."""
        return {
            "status": self.overall_status,
            "generated_at": self.generated_at,
            "integrations": [item.to_dict() for item in self.integrations],
        }

    def by_name(self, name: str) -> IntegrationStatus:
        """Return one integration by name."""
        for item in self.integrations:
            if item.name == name:
                return item
        raise KeyError(name)


GatewayLister = Callable[[], list[dict[str, Any]]]
HttpGet = Callable[..., Any]
ModuleChecker = Callable[[str], bool]

_SUPPORTED_LM_PROVIDERS = {"lm_studio", "ollama", "openai", "anthropic"}
_CLOUD_API_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}
_HDF5_TOOLS = {
    "hdf5_list_datasets",
    "hdf5_analyze_dataset",
    "hdf5_check_compression",
    "hdf5_optimize_chunking",
    "hdf5_analyze_file",
}
_PARQUET_TOOLS = {
    "parquet_analyze_schema",
    "parquet_query_data",
    "parquet_compute_statistics",
}
_DEFAULT_CLIO_CORE_PATH = Path("/home/akougkas/iowarp/clio-core")
_CLIO_CORE_ENV_VARS = [
    "CHI_SERVER_CONF",
    "WRP_RUNTIME_CONF",
    "CHI_REPO_PATH",
    "LD_LIBRARY_PATH",
]
_CLIO_CORE_CONFIG_CANDIDATES = [
    "docker/quickstart/chimaera.yaml",
    "context-runtime/config/chimaera_default.yaml",
    "docker/wrp_cte_bench/cte_config.yaml",
    "context-assimilation-engine/config/wrp_config_example.yaml",
    "context-transfer-engine/config/cae_example.yaml",
]
_CLIO_CORE_REPO_CONFIG_CANDIDATES = [
    "context-runtime/modules/chimaera_repo.yaml",
    "context-assimilation-engine/chimaera_repo.yaml",
    "context-transfer-engine/chimaera_repo.yaml",
]
_CLIO_CORE_BINARY_CANDIDATES = [
    "build/bin/{name}",
    "build/dev/bin/{name}",
    "build/local/bin/{name}",
    "install/bin/{name}",
    "installers/pip/iowarp_core/bin/{name}",
]


class RuntimeProbe:
    """Collect runtime status for configured CLIO integrations.

    Dependencies are injectable so tests can cover ready/degraded/unavailable
    paths without live LM Studio, Ollama, FastMCP, or clio-core runtimes.
    """

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        http_get: HttpGet | None = None,
        gateway_lister: GatewayLister | None = None,
        module_checker: ModuleChecker | None = None,
        lm_timeout: float = 1.0,
        api_timeout: float = 1.0,
        default_clio_core_path: str | Path | None = _DEFAULT_CLIO_CORE_PATH,
    ) -> None:
        self.env = env if env is not None else os.environ
        self.http_get = http_get or requests.get
        self.gateway_lister = gateway_lister or _list_gateway_capabilities
        self.module_checker = module_checker or _module_available
        self.lm_timeout = lm_timeout
        self.api_timeout = api_timeout
        self.default_clio_core_path = (
            Path(default_clio_core_path).expanduser()
            if default_clio_core_path is not None
            else None
        )

    def collect(
        self,
        *,
        api_state: IntegrationState | str | None = None,
        api_error: str | None = None,
    ) -> RuntimeReport:
        """Collect all currently supported integration statuses."""
        gateway_status = self.probe_gateway()
        gateway_tools = set(gateway_status.capabilities)
        integrations = [
            self.probe_lm_provider(),
            self.probe_arc(),
            self.probe_file_policy(),
            gateway_status,
            self.probe_hdf5(gateway_tools),
            self.probe_parquet(gateway_tools),
            self.probe_api(api_state=api_state, api_error=api_error),
            self.probe_clio_core(),
        ]
        return RuntimeReport(integrations=integrations)

    def probe_file_policy(self) -> IntegrationStatus:
        """Report local file access policy used by tools and direct answers."""
        try:
            policy = FileAccessPolicy.from_mapping(self.env)
        except FilePolicyError as exc:
            return IntegrationStatus(
                name="file_policy",
                state=IntegrationState.MISCONFIGURED,
                summary=exc.message,
                config_source=exc.field,
                next_action=exc.next_action,
                details=exc.to_result()["error"],
                required=True,
            )

        details = policy.to_dict()
        roots = ", ".join(details["allowed_roots"])
        config_source = (
            "env:CLIO_ALLOWED_ROOTS"
            if self.env.get("CLIO_ALLOWED_ROOTS", "").strip()
            else "default:cwd+/tmp"
        )
        max_source = (
            "env:CLIO_MAX_FILE_SIZE_BYTES"
            if self.env.get("CLIO_MAX_FILE_SIZE_BYTES", "").strip()
            else "default:1GiB"
        )
        symlink_source = (
            "env:CLIO_ALLOW_SYMLINKS"
            if self.env.get("CLIO_ALLOW_SYMLINKS", "").strip()
            else "default:deny-symlinks"
        )
        symlink_summary = "allowed" if policy.allow_symlinks else "denied"
        return IntegrationStatus(
            name="file_policy",
            state=IntegrationState.READY,
            summary=(
                f"Local file access allows roots [{roots}], max file size "
                f"{_format_bytes(policy.max_file_size_bytes)}, symlinks {symlink_summary}."
            ),
            config_source=f"{config_source}; {max_source}; {symlink_source}",
            next_action=(
                "Set CLIO_ALLOWED_ROOTS, CLIO_MAX_FILE_SIZE_BYTES, or "
                "CLIO_ALLOW_SYMLINKS to change local file policy."
            ),
            capabilities=["read-validation", "write-validation", "size-limit"],
            details=details,
            required=True,
        )

    def probe_lm_provider(self) -> IntegrationStatus:
        """Probe configured LM provider without constructing a DSPy agent."""
        try:
            config, source = self._load_lm_config()
        except ValueError as exc:
            return IntegrationStatus(
                name="lm_provider",
                state=IntegrationState.MISCONFIGURED,
                summary=str(exc),
                config_source="env:CLIO_LM_*",
                next_action="Fix CLIO_LM_* provider, endpoint, model, or API key settings.",
                required=True,
            )

        auth_mode = "api_key" if config.provider in _CLOUD_API_KEY_ENV else "local_token"
        if config.provider in _CLOUD_API_KEY_ENV:
            return IntegrationStatus(
                name="lm_provider",
                state=IntegrationState.SKIPPED,
                summary=f"{config.provider} is configured; live cloud probe was skipped.",
                config_source=source,
                next_action="Run a query or enable a deployment-specific authenticated probe.",
                endpoint=config.api_base,
                auth_mode=auth_mode,
                capabilities=["chat-completions"],
                details={"provider": config.provider, "model": config.model},
                required=True,
            )

        models_url = config.api_base.rstrip("/") + "/models"
        try:
            response = self.http_get(models_url, timeout=self.lm_timeout)
        except Exception as exc:
            return IntegrationStatus(
                name="lm_provider",
                state=IntegrationState.UNAVAILABLE,
                summary=f"{config.provider} endpoint is not reachable: {exc}",
                config_source=source,
                next_action=f"Start {config.provider} or set CLIO_LM_API_BASE to a reachable URL.",
                endpoint=config.api_base,
                auth_mode=auth_mode,
                details={"provider": config.provider, "model": config.model},
                required=True,
            )

        status_code = getattr(response, "status_code", 200)
        if status_code in {401, 403}:
            return IntegrationStatus(
                name="lm_provider",
                state=IntegrationState.MISCONFIGURED,
                summary=f"{config.provider} rejected authentication with HTTP {status_code}.",
                config_source=source,
                next_action="Check CLIO_LM_API_KEY or provider authentication settings.",
                endpoint=config.api_base,
                auth_mode=auth_mode,
                details={"provider": config.provider, "model": config.model},
                required=True,
            )
        if status_code >= 400:
            return IntegrationStatus(
                name="lm_provider",
                state=IntegrationState.DEGRADED,
                summary=f"{config.provider} returned HTTP {status_code} for model listing.",
                config_source=source,
                next_action="Inspect the provider logs and verify the OpenAI-compatible /models API.",
                endpoint=config.api_base,
                auth_mode=auth_mode,
                details={"provider": config.provider, "model": config.model},
                required=True,
            )

        models = self._extract_models(response)
        if not models:
            return IntegrationStatus(
                name="lm_provider",
                state=IntegrationState.DEGRADED,
                summary=f"{config.provider} is reachable but reported no loaded models.",
                config_source=source,
                next_action="Load a chat/instruct model in the local provider.",
                endpoint=config.api_base,
                auth_mode=auth_mode,
                capabilities=["models"],
                details={"provider": config.provider, "configured_model": config.model},
                required=True,
            )

        return IntegrationStatus(
            name="lm_provider",
            state=IntegrationState.READY,
            summary=f"{config.provider} is reachable with {len(models)} model(s).",
            config_source=source,
            next_action="No action required.",
            endpoint=config.api_base,
            auth_mode=auth_mode,
            capabilities=["chat-completions", "models"],
            details={
                "provider": config.provider,
                "configured_model": config.model,
                "model_count": len(models),
                "models": models[:10],
            },
            required=True,
        )

    def probe_arc(self) -> IntegrationStatus:
        """Probe local ARC persistence path readiness."""
        base_dir = Path(self.env.get("CLIO_DATA_DIR", ".clio_agent"))
        arc_dir = base_dir / "arc"
        source = "env:CLIO_DATA_DIR" if "CLIO_DATA_DIR" in self.env else "default:.clio_agent"
        try:
            arc_dir.mkdir(parents=True, exist_ok=True)
            probe_file = arc_dir / ".doctor_probe"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink(missing_ok=True)
        except Exception as exc:
            return IntegrationStatus(
                name="arc",
                state=IntegrationState.UNAVAILABLE,
                summary=f"ARC local persistence is not writable: {exc}",
                config_source=source,
                next_action="Set CLIO_DATA_DIR to a writable directory.",
                endpoint=str(arc_dir),
                fallback="none",
                capabilities=["local-persistence"],
                required=True,
            )

        return IntegrationStatus(
            name="arc",
            state=IntegrationState.READY,
            summary="ARC local persistence is writable.",
            config_source=source,
            next_action="No action required for local mode; configure CTE when that adapter is enabled.",
            endpoint=str(arc_dir),
            fallback="local",
            capabilities=["conversations", "invocations", "metrics", "profiles", "variants"],
            details={"storage_mode": "local"},
            required=True,
        )

    def probe_gateway(self) -> IntegrationStatus:
        """Probe FastMCP gateway tool discovery."""
        try:
            capabilities = self.gateway_lister()
        except Exception as exc:
            return IntegrationStatus(
                name="gateway",
                state=IntegrationState.UNAVAILABLE,
                summary=f"FastMCP gateway discovery failed: {exc}",
                config_source="in-process:clio_agent.tools.gateway",
                next_action="Inspect gateway import errors and FastMCP server construction.",
                fallback="none",
                required=True,
            )

        tool_names = sorted(
            item["name"] for item in capabilities if isinstance(item.get("name"), str)
        )
        expected = _HDF5_TOOLS | _PARQUET_TOOLS
        missing = sorted(expected - set(tool_names))
        state = IntegrationState.READY if not missing else IntegrationState.DEGRADED
        summary = (
            f"Gateway exposes {len(tool_names)} expected tool(s)."
            if not missing
            else f"Gateway is missing expected tool(s): {', '.join(missing)}."
        )
        next_action = (
            "No action required."
            if not missing
            else "Verify HDF5 and Parquet servers are mounted with stable namespaces."
        )
        return IntegrationStatus(
            name="gateway",
            state=state,
            summary=summary,
            config_source="in-process:clio_agent.tools.gateway",
            next_action=next_action,
            capabilities=tool_names,
            details={"missing_tools": missing},
            required=True,
        )

    def probe_hdf5(self, gateway_tools: set[str]) -> IntegrationStatus:
        """Probe HDF5 backend imports and gateway exposure."""
        if not self.module_checker("h5py"):
            return IntegrationStatus(
                name="hdf5",
                state=IntegrationState.UNAVAILABLE,
                summary="h5py is not importable.",
                config_source="python import:h5py",
                next_action="Install the HDF5 runtime dependency with the project extras.",
                capabilities=[],
                required=True,
            )
        missing = sorted(_HDF5_TOOLS - gateway_tools)
        if missing:
            return IntegrationStatus(
                name="hdf5",
                state=IntegrationState.DEGRADED,
                summary=f"h5py is available but gateway tools are missing: {', '.join(missing)}.",
                config_source="python import:h5py; in-process gateway",
                next_action="Fix the HDF5 FastMCP server mount before relying on HDF5 tools.",
                capabilities=sorted(_HDF5_TOOLS & gateway_tools),
                details={"missing_tools": missing},
                required=True,
            )
        return IntegrationStatus(
            name="hdf5",
            state=IntegrationState.READY,
            summary="HDF5 backend and gateway tools are available.",
            config_source="python import:h5py; in-process gateway",
            next_action="No action required.",
            capabilities=sorted(_HDF5_TOOLS),
            required=True,
        )

    def probe_parquet(self, gateway_tools: set[str]) -> IntegrationStatus:
        """Probe Parquet backend imports and gateway exposure."""
        if not self.module_checker("pyarrow.parquet"):
            return IntegrationStatus(
                name="parquet",
                state=IntegrationState.UNAVAILABLE,
                summary="pyarrow.parquet is not importable.",
                config_source="python import:pyarrow.parquet",
                next_action="Install the Parquet runtime dependency with the project extras.",
                capabilities=[],
                required=True,
            )
        missing = sorted(_PARQUET_TOOLS - gateway_tools)
        if missing:
            return IntegrationStatus(
                name="parquet",
                state=IntegrationState.DEGRADED,
                summary=(
                    "pyarrow.parquet is available but gateway tools are missing: "
                    f"{', '.join(missing)}."
                ),
                config_source="python import:pyarrow.parquet; in-process gateway",
                next_action="Fix the Parquet FastMCP server mount before relying on Parquet tools.",
                capabilities=sorted(_PARQUET_TOOLS & gateway_tools),
                details={"missing_tools": missing},
                required=True,
            )
        return IntegrationStatus(
            name="parquet",
            state=IntegrationState.READY,
            summary="Parquet backend and gateway tools are available.",
            config_source="python import:pyarrow.parquet; in-process gateway",
            next_action="No action required.",
            capabilities=sorted(_PARQUET_TOOLS),
            required=True,
        )

    def probe_api(
        self,
        *,
        api_state: IntegrationState | str | None = None,
        api_error: str | None = None,
    ) -> IntegrationStatus:
        """Probe API status from current app state or an optional configured endpoint."""
        capabilities = ["/health", "/query", "/experts", "/metrics"]
        if api_state is not None:
            state = IntegrationState(api_state)
            return IntegrationStatus(
                name="api",
                state=state,
                summary=(
                    "API application is running in this process."
                    if state == IntegrationState.READY
                    else f"API application is degraded: {api_error or 'unknown startup error'}"
                ),
                config_source="current FastAPI app state",
                next_action=(
                    "No action required."
                    if state == IntegrationState.READY
                    else "Fix the API startup error, then restart the service."
                ),
                endpoint="in-process",
                capabilities=capabilities,
                details={"error": api_error} if api_error else {},
                required=True,
            )

        endpoint = self.env.get("CLIO_API_BASE")
        if not endpoint:
            return IntegrationStatus(
                name="api",
                state=IntegrationState.SKIPPED,
                summary="No API endpoint configured for live probing.",
                config_source="default:no CLIO_API_BASE",
                next_action="Start the API or set CLIO_API_BASE for live API health checks.",
                capabilities=capabilities,
                required=True,
            )

        health_url = endpoint.rstrip("/") + "/health"
        try:
            response = self.http_get(health_url, timeout=self.api_timeout)
        except Exception as exc:
            return IntegrationStatus(
                name="api",
                state=IntegrationState.UNAVAILABLE,
                summary=f"API endpoint is not reachable: {exc}",
                config_source="env:CLIO_API_BASE",
                next_action="Start the API service or correct CLIO_API_BASE.",
                endpoint=endpoint,
                capabilities=capabilities,
                required=True,
            )

        status_code = getattr(response, "status_code", 200)
        api_body_status = ""
        try:
            body = response.json()
            if isinstance(body, dict):
                api_body_status = str(body.get("status", ""))
        except Exception:
            api_body_status = ""

        if status_code >= 500 or api_body_status == "degraded":
            return IntegrationStatus(
                name="api",
                state=IntegrationState.DEGRADED,
                summary=f"API health returned HTTP {status_code} status={api_body_status or '?'}",
                config_source="env:CLIO_API_BASE",
                next_action="Inspect API logs and startup health detail.",
                endpoint=endpoint,
                capabilities=capabilities,
                details={"http_status": status_code, "health_status": api_body_status},
                required=True,
            )

        return IntegrationStatus(
            name="api",
            state=IntegrationState.READY,
            summary="API health endpoint is reachable.",
            config_source="env:CLIO_API_BASE",
            next_action="No action required.",
            endpoint=endpoint,
            capabilities=capabilities,
            details={"http_status": status_code, "health_status": api_body_status},
            required=True,
        )

    def probe_clio_core(self) -> IntegrationStatus:
        """Report optional clio-core configuration without starting services."""
        core_path, source, explicit = self._resolve_clio_core_path()
        if core_path is None:
            return IntegrationStatus(
                name="clio_core",
                state=IntegrationState.SKIPPED,
                summary="Optional clio-core runtime probe is not configured.",
                config_source=source,
                next_action=(
                    "Set CLIO_CORE_PATH or CHI_REPO_PATH when enabling clio-core probing."
                ),
                capabilities=[],
                details={"suggested_env": _CLIO_CORE_ENV_VARS},
                required=False,
            )

        if not core_path.exists():
            return IntegrationStatus(
                name="clio_core",
                state=IntegrationState.MISCONFIGURED,
                summary=f"Configured clio-core path does not exist: {core_path}",
                config_source=source,
                next_action="Fix CLIO_CORE_PATH or CHI_REPO_PATH.",
                endpoint=str(core_path),
                details={"suggested_env": _CLIO_CORE_ENV_VARS},
                required=False,
            )
        if not core_path.is_dir():
            return IntegrationStatus(
                name="clio_core",
                state=IntegrationState.MISCONFIGURED,
                summary=f"Configured clio-core path is not a directory: {core_path}",
                config_source=source,
                next_action="Set CLIO_CORE_PATH or CHI_REPO_PATH to the clio-core repository root.",
                endpoint=str(core_path),
                details={"suggested_env": _CLIO_CORE_ENV_VARS},
                required=False,
            )

        env_paths = self._clio_core_env_details()
        missing_env_paths = [
            item for item in env_paths if item["configured"] and item.get("exists") is False
        ]
        if missing_env_paths:
            missing_env_summary = ", ".join(
                f"{item['name']}={item['value']}" for item in missing_env_paths
            )
            return IntegrationStatus(
                name="clio_core",
                state=IntegrationState.MISCONFIGURED,
                summary=f"Configured clio-core env path(s) do not exist: {missing_env_summary}",
                config_source=source,
                next_action="Fix or unset the missing clio-core environment path(s).",
                endpoint=str(core_path),
                details={
                    "suggested_env": _CLIO_CORE_ENV_VARS,
                    "env": env_paths,
                    "non_destructive": True,
                },
                required=False,
            )

        chimaera_bins = self._find_binary_candidates(core_path, "chimaera", "CLIO_CHIMAERA_BIN")
        cae_bins = self._find_binary_candidates(core_path, "wrp_cae_omni", "CLIO_WRP_CAE_OMNI_BIN")
        config_candidates = self._find_existing_relative(core_path, _CLIO_CORE_CONFIG_CANDIDATES)
        repo_configs = self._find_existing_relative(core_path, _CLIO_CORE_REPO_CONFIG_CANDIDATES)
        visualizer = self._probe_visualizer(core_path)

        capabilities = ["path-detected"]
        if chimaera_bins:
            capabilities.append("chimaera-cli")
        if cae_bins:
            capabilities.append("wrp_cae_omni")
        if config_candidates:
            capabilities.append("yaml-config")
        if repo_configs:
            capabilities.append("chimaera-repo-config")
        if visualizer.get("source_detected"):
            capabilities.append("visualizer-source")
        if visualizer.get("state") == "ready":
            capabilities.append("visualizer-status")

        missing_capabilities: list[str] = []
        if not chimaera_bins:
            missing_capabilities.append("chimaera binary")
        if not config_candidates and not any(
            item["name"] in {"CHI_SERVER_CONF", "WRP_RUNTIME_CONF"} and item["configured"]
            for item in env_paths
        ):
            missing_capabilities.append("runtime YAML config")
        if visualizer.get("state") == "unavailable":
            missing_capabilities.append("visualizer status endpoint")

        details = {
            "suggested_env": _CLIO_CORE_ENV_VARS,
            "env": env_paths,
            "chimaera_binaries": chimaera_bins,
            "wrp_cae_omni_binaries": cae_bins,
            "config_candidates": config_candidates,
            "repo_configs": repo_configs,
            "visualizer": visualizer,
            "non_destructive": True,
            "explicit_path": explicit,
        }

        if missing_capabilities:
            return IntegrationStatus(
                name="clio_core",
                state=IntegrationState.DEGRADED,
                summary=(
                    "clio-core path exists but discovery is incomplete: "
                    f"{', '.join(missing_capabilities)}."
                ),
                config_source=source,
                next_action=(
                    "Build/install clio-core or set CLIO_CHIMAERA_BIN, CHI_SERVER_CONF, "
                    "WRP_RUNTIME_CONF, CHI_REPO_PATH, and LD_LIBRARY_PATH as needed."
                ),
                endpoint=str(core_path),
                capabilities=capabilities,
                details=details,
                required=False,
            )

        return IntegrationStatus(
            name="clio_core",
            state=IntegrationState.READY,
            summary="clio-core discovery found a repository path, chimaera binary, and config.",
            config_source=source,
            next_action="No action required for discovery; start clio-core services explicitly when needed.",
            endpoint=str(core_path),
            capabilities=capabilities,
            details=details,
            required=False,
        )

    def _resolve_clio_core_path(self) -> tuple[Path | None, str, bool]:
        configured_path = self.env.get("CLIO_CORE_PATH") or self.env.get("CHI_REPO_PATH")
        if configured_path:
            return Path(configured_path).expanduser(), "env:CLIO_CORE_PATH/CHI_REPO_PATH", True
        if self.default_clio_core_path and self.default_clio_core_path.exists():
            return self.default_clio_core_path, f"default:{self.default_clio_core_path}", False
        return None, "default:not configured", False

    def _clio_core_env_details(self) -> list[dict[str, Any]]:
        details: list[dict[str, Any]] = []
        for name in _CLIO_CORE_ENV_VARS:
            value = self.env.get(name, "")
            item: dict[str, Any] = {"name": name, "configured": bool(value)}
            if value:
                item["value"] = value
                if name == "LD_LIBRARY_PATH":
                    paths = [Path(part).expanduser() for part in value.split(os.pathsep) if part]
                    item["paths"] = [str(path) for path in paths]
                    item["exists"] = all(path.exists() for path in paths) if paths else False
                else:
                    path = Path(value).expanduser()
                    item["value"] = str(path)
                    item["exists"] = path.exists()
            details.append(item)
        return details

    def _find_binary_candidates(self, core_path: Path, name: str, env_key: str) -> list[str]:
        candidates: list[Path] = []
        env_value = self.env.get(env_key)
        if env_value:
            candidates.append(Path(env_value).expanduser())
        path_candidate = shutil.which(name, path=self.env.get("PATH"))
        if path_candidate:
            candidates.append(Path(path_candidate))
        for pattern in _CLIO_CORE_BINARY_CANDIDATES:
            candidates.append(core_path / pattern.format(name=name))
        return _existing_unique_paths(candidates, executable=True)

    @staticmethod
    def _find_existing_relative(core_path: Path, relative_paths: list[str]) -> list[str]:
        return _existing_unique_paths([core_path / item for item in relative_paths])

    def _probe_visualizer(self, core_path: Path) -> dict[str, Any]:
        source_detected = any(
            (core_path / item).exists()
            for item in (
                "context-visualizer/pyproject.toml",
                "context-visualizer/context_visualizer/chimaera_client.py",
            )
        )
        visualizer_url = self.env.get("CLIO_CORE_VISUALIZER_URL") or self.env.get(
            "CLIO_VISUALIZER_URL"
        )
        result: dict[str, Any] = {
            "source_detected": source_detected,
            "configured_url": visualizer_url or "",
            "state": "skipped",
        }
        if not visualizer_url:
            return result

        status_url = visualizer_url.rstrip("/") + "/status"
        result["status_url"] = status_url
        try:
            response = self.http_get(status_url, timeout=self.api_timeout)
        except Exception as exc:
            result["state"] = "unavailable"
            result["error"] = str(exc)
            return result

        status_code = getattr(response, "status_code", 200)
        result["http_status"] = status_code
        result["state"] = "ready" if status_code < 400 else "unavailable"
        return result

    def _load_lm_config(self) -> tuple[LMProviderConfig, str]:
        provider = self.env.get("CLIO_LM_PROVIDER", "lm_studio")
        if provider not in _SUPPORTED_LM_PROVIDERS:
            raise ValueError(
                f"Unsupported CLIO_LM_PROVIDER '{provider}'. "
                f"Supported providers: {', '.join(sorted(_SUPPORTED_LM_PROVIDERS))}."
            )

        defaults = PROVIDER_DEFAULTS[provider]
        api_base = self.env.get("CLIO_LM_API_BASE", defaults["api_base"])
        model = self.env.get("CLIO_LM_MODEL", defaults["model"])
        api_key = self.env.get("CLIO_LM_API_KEY", "")
        key_source = "env:CLIO_LM_API_KEY" if api_key else ""
        native_key_env = _CLOUD_API_KEY_ENV.get(provider)
        if not api_key and native_key_env:
            api_key = self.env.get(native_key_env, "")
            key_source = f"env:{native_key_env}" if api_key else ""
        if not api_key:
            api_key = defaults["api_key"]
            key_source = f"default:{provider}"

        if provider in _CLOUD_API_KEY_ENV and not api_key:
            raise ValueError(
                f"Cloud provider '{provider}' requires CLIO_LM_API_KEY "
                f"or {_CLOUD_API_KEY_ENV[provider]}."
            )

        config = LMProviderConfig(
            provider=provider,  # type: ignore[arg-type]
            api_base=api_base,
            model=model,
            api_key=api_key,
            temperature=self._float_env("CLIO_LM_TEMPERATURE", 1.0),
            max_tokens=self._int_env("CLIO_LM_MAX_TOKENS", 32000),
            environment=self.env.get("CLIO_ENVIRONMENT", "dev"),
        )
        source_parts = [
            "env:CLIO_LM_PROVIDER" if "CLIO_LM_PROVIDER" in self.env else f"default:{provider}",
            "env:CLIO_LM_API_BASE" if "CLIO_LM_API_BASE" in self.env else f"default:{provider}",
            "env:CLIO_LM_MODEL" if "CLIO_LM_MODEL" in self.env else f"default:{provider}",
            key_source,
        ]
        return config, ", ".join(part for part in source_parts if part)

    def _float_env(self, key: str, default: float) -> float:
        value = self.env.get(key)
        if value is None:
            return default
        try:
            return float(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be a float, got {value!r}.") from exc

    def _int_env(self, key: str, default: int) -> int:
        value = self.env.get(key)
        if value is None:
            return default
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer, got {value!r}.") from exc

    @staticmethod
    def _extract_models(response: Any) -> list[str]:
        try:
            data = response.json()
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        raw_models = data.get("data", [])
        if not isinstance(raw_models, list):
            return []
        models: list[str] = []
        for item in raw_models:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                models.append(item["id"])
        return models


def collect_runtime_status(
    *,
    api_state: IntegrationState | str | None = None,
    api_error: str | None = None,
    env: Mapping[str, str] | None = None,
    lm_timeout: float = 1.0,
) -> RuntimeReport:
    """Collect a runtime status report using default probes."""
    probe = RuntimeProbe(env=env, lm_timeout=lm_timeout)
    return probe.collect(api_state=api_state, api_error=api_error)


def _module_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def _existing_unique_paths(paths: list[Path], *, executable: bool = False) -> list[str]:
    seen: set[str] = set()
    results: list[str] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        if executable and not os.access(path, os.X_OK):
            continue
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            results.append(resolved)
    return results


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _list_gateway_capabilities() -> list[dict[str, Any]]:
    import asyncio
    import concurrent.futures

    from fastmcp import Client

    from clio_agent.tools.gateway import gateway

    async def _list_tools() -> list[Any]:
        async with Client(gateway) as client:
            return await client.list_tools()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        tools = asyncio.run(_list_tools())
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            tools = pool.submit(lambda: asyncio.run(_list_tools())).result()

    capabilities = []
    for tool in sorted(tools, key=lambda item: item.name):
        description = tool.description or ""
        first_sentence = description.split(".")[0].strip() + "." if description else ""
        if tool.name.startswith("hdf5_"):
            server = "hdf5"
        elif tool.name.startswith("parquet_"):
            server = "parquet"
        else:
            server = "unknown"
        capabilities.append(
            {
                "name": tool.name,
                "description": first_sentence,
                "server": server,
            }
        )
    return capabilities
