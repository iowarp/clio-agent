"""Runtime integration status probes for CLIO Agent.

The doctor path should report what is actually configured and reachable without
booting a full agent or requiring live IOWarp/clio-core services.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping

import requests

from clio_agent.config import (
    _CLOUD_API_KEY_ENV as _CONFIG_CLOUD_API_KEY_ENV,
)
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
PortChecker = Callable[[int], bool]

# Derived from the provider registry: the wire kinds with entries in
# PROVIDER_DEFAULTS, including the Codex CustomLLM entry.
_SUPPORTED_LM_PROVIDERS = frozenset(PROVIDER_DEFAULTS.keys())
_CLOUD_API_KEY_ENV = _CONFIG_CLOUD_API_KEY_ENV


class ModelDiscoverySchemaError(ValueError):
    """Raised when an OpenAI-compatible /models response is malformed."""

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


# Maps a gateway tool namespace (the prefix before the first underscore in a
# mounted tool name) to the Python module its server depends on. This drives
# backend verification for servers that are ACTUALLY mounted on the active
# gateway — it does not declare any server as universally required. Namespaces
# absent from the gateway produce no status at all.
_DATA_BACKEND_MODULES = {
    "hdf5": "h5py",
    "parquet": "pyarrow.parquet",
}

# The gact /v1 surface the doctor probes (#800). The legacy /health /query
# /experts API is not what production serves.
_GACT_API_ENDPOINTS = ["/v1/health", "/v1/capabilities"]

# How many trailing lines of ~/.clio/clio-runtime.log to surface when the
# clio-core daemon is down.
_CTE_LOG_TAIL_LINES = 20


@dataclass(frozen=True)
class CTERuntimeHealth:
    """Observed state of the production clio-core runtime.

    The production deployment is the pip ``iowarp_core`` package plus the
    shared ``clio_run`` daemon (see :mod:`clio_agent.arc.storage`); this is the
    single reality both :meth:`RuntimeProbe.probe_arc` (CTE backend) and
    :meth:`RuntimeProbe.probe_clio_core` gate on.
    """

    installed: bool
    port: int
    daemon_alive: bool
    daemon_pid: int | None
    daemon_pid_alive: bool | None
    log_path: str
    log_tail: list[str]
    reason: str | None

    @property
    def healthy(self) -> bool:
        """Whether the pip runtime is present and the daemon is listening."""
        return self.installed and self.daemon_alive

    def to_details(self) -> dict[str, Any]:
        """Shared structured detail payload for doctor statuses."""
        details: dict[str, Any] = {
            "iowarp_core_installed": self.installed,
            "port": self.port,
            "daemon_alive": self.daemon_alive,
        }
        if self.daemon_pid is not None:
            details["daemon_pid"] = self.daemon_pid
            details["daemon_pid_alive"] = self.daemon_pid_alive
        if self.reason is not None:
            details["reason"] = self.reason
            details["log_path"] = self.log_path
            details["log_tail"] = self.log_tail
        return details


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
        port_checker: PortChecker | None = None,
        clio_runtime_dir: str | Path | None = None,
    ) -> None:
        self.env = env if env is not None else os.environ
        self.http_get = http_get or requests.get
        self.gateway_lister = gateway_lister or _list_gateway_capabilities
        self.module_checker = module_checker or _module_available
        self.lm_timeout = lm_timeout
        self.api_timeout = api_timeout
        # Default resolved lazily to arc.storage._runtime_alive so unit tests
        # never open real sockets and module import stays light.
        self._port_checker = port_checker
        # Where the shared clio-core daemon keeps its pidfile and log; matches
        # arc.storage._daemon_pidfile() / _spawn_runtime_daemon() (~/.clio).
        self.clio_runtime_dir = (
            Path(clio_runtime_dir).expanduser()
            if clio_runtime_dir is not None
            else Path.home() / ".clio"
        )
        self._cte_runtime: CTERuntimeHealth | None = None

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
            *self.probe_data_backends(gateway_tools),
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
        if config.provider == "argonne":
            auth_mode = "globus_token"
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

        # Argonne / ALCF: never live-probe — that would force an OAuth
        # round-trip on every /doctor or /health hit. Report instead on
        # whether stored tokens exist + whether globus-sdk is importable.
        if config.provider == "argonne":
            return self._probe_argonne(config, source)

        models_url = config.api_base.rstrip("/") + "/models"
        try:
            response = self.http_get(models_url, timeout=self.lm_timeout)
        except Exception as exc:  # noqa: BLE001 - surfaced in IntegrationStatus (degraded doctor row)
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

        try:
            models = self._extract_models(response)
        except ModelDiscoverySchemaError as exc:
            return IntegrationStatus(
                name="lm_provider",
                state=IntegrationState.DEGRADED,
                summary=f"{config.provider} returned a malformed model listing: {exc}",
                config_source=source,
                next_action="Verify the provider exposes an OpenAI-compatible /models response.",
                endpoint=config.api_base,
                auth_mode=auth_mode,
                capabilities=["models"],
                details={
                    "provider": config.provider,
                    "configured_model": config.model,
                    "model_discovery_error": exc.code,
                },
                required=True,
            )
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

    def _probe_argonne(
        self,
        config: LMProviderConfig,
        source: str,
    ) -> IntegrationStatus:
        """Cheap-status report for the ALCF inference gateway.

        We never trigger globus OAuth here — that's an interactive flow
        the user only wants when they explicitly opt in (CLI command or
        TUI button). Instead we look at:

          - Is ``globus-sdk`` importable? (UNAVAILABLE if not.)
          - Do tokens exist on disk? (MISCONFIGURED if not — needs a
            one-time ``authenticate``.)
          - Otherwise SKIPPED with a "ready, run a query to verify"
            summary, mirroring the OpenAI/Anthropic probe path.
        """
        details = {"provider": "argonne", "model": config.model}
        try:
            from clio_agent.providers import argonne_auth  # noqa: PLC0415
        except Exception as exc:  # pragma: no cover - import bug  # noqa: BLE001 - surfaced in IntegrationStatus (degraded doctor row)
            return IntegrationStatus(
                name="lm_provider",
                state=IntegrationState.UNAVAILABLE,
                summary=f"argonne provider module failed to import: {exc}",
                config_source=source,
                next_action="Reinstall clio-agent or check the providers package.",
                endpoint=config.api_base,
                auth_mode="globus_token",
                details=details,
                required=True,
            )

        if not self.module_checker("globus_sdk"):
            return IntegrationStatus(
                name="lm_provider",
                state=IntegrationState.UNAVAILABLE,
                summary="globus-sdk is not importable; ALCF tokens cannot be minted.",
                config_source=source,
                next_action="Install with: pip install 'clio-agent[argonne]'",
                endpoint=config.api_base,
                auth_mode="globus_token",
                details=details,
                required=True,
            )

        if not argonne_auth.tokens_exist():
            return IntegrationStatus(
                name="lm_provider",
                state=IntegrationState.MISCONFIGURED,
                summary="ALCF provider selected but no Globus tokens are stored.",
                config_source=source,
                next_action=("Run once: python -m clio_agent.providers.argonne_auth authenticate"),
                endpoint=config.api_base,
                auth_mode="globus_token",
                details=details,
                required=True,
            )

        return IntegrationStatus(
            name="lm_provider",
            state=IntegrationState.SKIPPED,
            summary=(
                "argonne is configured with stored Globus tokens; "
                "live probe skipped to avoid spurious OAuth refreshes."
            ),
            config_source=source,
            next_action="Run a query to verify the token still validates.",
            endpoint=config.api_base,
            auth_mode="globus_token",
            capabilities=["chat-completions"],
            details=details,
            required=True,
        )

    def _arc_backend(self) -> tuple[str, str]:
        """Return the selected ARC backend and its config source.

        Mirrors :func:`clio_agent.arc.storage.make_arc_store` (env
        ``CLIO_ARC_STORE``, default ``cte``) so the doctor reports the backend
        the runtime will actually construct, not a hardcoded assumption (#800).
        """
        backend = self.env.get("CLIO_ARC_STORE", "cte").strip().lower()
        source = "env:CLIO_ARC_STORE" if "CLIO_ARC_STORE" in self.env else "default:cte"
        return backend, source

    def _probe_cte_runtime(self) -> CTERuntimeHealth:
        """Probe the production clio-core runtime: pip package + shared daemon.

        Shared by :meth:`probe_arc` (CTE backend) and :meth:`probe_clio_core`
        so both report on one reality. Uses the same helpers the runtime
        lifecycle in :mod:`clio_agent.arc.storage` uses: the resolved RPC port,
        a socket liveness check, the daemon pidfile, and — on failure — the
        tail of ``~/.clio/clio-runtime.log``. Memoized per probe instance so
        one ``collect()`` opens at most one socket.
        """
        if self._cte_runtime is not None:
            return self._cte_runtime
        from clio_agent.arc import storage as arc_storage  # noqa: PLC0415 - keep import light

        installed = self.module_checker("iowarp_core")
        port = arc_storage._resolve_runtime_port(self.env.get("CLIO_ARC_STORE_CONFIG", ""))
        port_checker = self._port_checker or arc_storage._runtime_alive
        daemon_alive = bool(port_checker(port))

        daemon_pid: int | None = None
        daemon_pid_alive: bool | None = None
        try:
            parts = (self.clio_runtime_dir / "clio-runtime.pid").read_text("utf-8").split()
        except OSError:
            parts = []
        if parts:
            with contextlib.suppress(ValueError):
                daemon_pid = int(parts[0])
        if daemon_pid is not None:
            recorded: float | None = None
            if len(parts) > 1:
                with contextlib.suppress(ValueError):
                    recorded = float(parts[1])
            daemon_pid_alive = arc_storage._pid_alive(daemon_pid, recorded)

        if not installed:
            reason: str | None = "iowarp_core_not_installed"
        elif not daemon_alive:
            reason = "cte_daemon_not_listening"
        else:
            reason = None

        log_path = self.clio_runtime_dir / "clio-runtime.log"
        log_tail: list[str] = []
        if reason is not None and log_path.is_file():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                log_tail = lines[-_CTE_LOG_TAIL_LINES:]
            except OSError:
                log_tail = []

        self._cte_runtime = CTERuntimeHealth(
            installed=installed,
            port=port,
            daemon_alive=daemon_alive,
            daemon_pid=daemon_pid,
            daemon_pid_alive=daemon_pid_alive,
            log_path=str(log_path),
            log_tail=log_tail,
            reason=reason,
        )
        return self._cte_runtime

    def probe_arc(self) -> IntegrationStatus:
        """Probe the ARC persistence backend that is actually selected.

        CTE (the default backend) is probed for real: pip ``iowarp_core``
        presence plus shared-daemon liveness — a broken CTE install goes red
        instead of green-on-a-hardcoded-'local' (#800). The explicit local
        backend keeps the directory writability check.
        """
        backend, source = self._arc_backend()
        if backend == "local":
            return self._probe_arc_local(source)
        if backend == "cte":
            return self._probe_arc_cte(source)
        return IntegrationStatus(
            name="arc",
            state=IntegrationState.MISCONFIGURED,
            summary=f"Unknown CLIO_ARC_STORE {backend!r}; expected 'cte' or 'local'.",
            config_source=source,
            next_action="Set CLIO_ARC_STORE to 'cte' or 'local'.",
            fallback="none",
            details={"reason": "unknown_arc_backend", "configured_backend": backend},
            required=True,
        )

    def _probe_arc_cte(self, source: str) -> IntegrationStatus:
        """Probe the clio-core CTE backend (pip runtime + shared daemon)."""
        runtime = self._probe_cte_runtime()
        details = {"storage_mode": "cte", **runtime.to_details()}
        endpoint = f"127.0.0.1:{runtime.port}"
        if not runtime.installed:
            return IntegrationStatus(
                name="arc",
                state=IntegrationState.UNAVAILABLE,
                summary=(
                    "ARC is configured for the clio-core CTE backend but the "
                    "iowarp_core pip package is not installed."
                ),
                config_source=source,
                next_action=(
                    "Install the iowarp-core pip package, or set CLIO_ARC_STORE=local "
                    "to deliberately use the LocalFS backend."
                ),
                endpoint=endpoint,
                fallback="none",
                details=details,
                required=True,
            )
        if not runtime.daemon_alive:
            return IntegrationStatus(
                name="arc",
                state=IntegrationState.UNAVAILABLE,
                summary=(
                    "ARC is configured for the clio-core CTE backend but the shared "
                    f"clio-core daemon is not listening on port {runtime.port}."
                ),
                config_source=source,
                next_action=(
                    "Start the shared clio-core daemon (clio start / clio_run start) "
                    f"or set CLIO_ARC_STORE=local; see {runtime.log_path}."
                ),
                endpoint=endpoint,
                fallback="none",
                details=details,
                required=True,
            )
        return IntegrationStatus(
            name="arc",
            state=IntegrationState.READY,
            summary=(
                "ARC CTE backend is live: iowarp_core is installed and the shared "
                f"clio-core daemon is listening on port {runtime.port}."
            ),
            config_source=source,
            next_action="No action required.",
            endpoint=endpoint,
            fallback="none",
            capabilities=[
                "conversations",
                "invocations",
                "metrics",
                "profiles",
                "variants",
                "semantic-search",
            ],
            details=details,
            required=True,
        )

    def _probe_arc_local(self, backend_source: str) -> IntegrationStatus:
        """Probe local ARC persistence path readiness (explicit local backend)."""
        base_dir = Path(self.env.get("CLIO_DATA_DIR", ".clio/agent"))
        arc_dir = base_dir / "arc"
        dir_source = "env:CLIO_DATA_DIR" if "CLIO_DATA_DIR" in self.env else "default:.clio/agent"
        source = f"{backend_source}; {dir_source}"
        try:
            arc_dir.mkdir(parents=True, exist_ok=True)
            probe_file = arc_dir / ".doctor_probe"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - surfaced in IntegrationStatus (degraded doctor row)
            return IntegrationStatus(
                name="arc",
                state=IntegrationState.UNAVAILABLE,
                summary=f"ARC local persistence is not writable: {exc}",
                config_source=source,
                next_action="Set CLIO_DATA_DIR to a writable directory.",
                endpoint=str(arc_dir),
                fallback="none",
                capabilities=["local-persistence"],
                details={
                    "storage_mode": "local",
                    "reason": "arc_dir_not_writable",
                    "error": str(exc),
                },
                required=True,
            )

        return IntegrationStatus(
            name="arc",
            state=IntegrationState.READY,
            summary="ARC local persistence is writable.",
            config_source=source,
            next_action="No action required for local mode; set CLIO_ARC_STORE=cte for clio-core.",
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
        except Exception as exc:  # noqa: BLE001 - surfaced in IntegrationStatus (degraded doctor row)
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
        # The healthy "expected" set is whatever the active gateway actually
        # mounts — there is no universal tool requirement. A gateway that
        # exposes at least one tool is READY; an empty gateway is genuinely
        # broken (no servers mounted) and is DEGRADED.
        namespaces = sorted({name.split("_", 1)[0] for name in tool_names if "_" in name})
        if tool_names:
            return IntegrationStatus(
                name="gateway",
                state=IntegrationState.READY,
                summary=(
                    f"Gateway exposes {len(tool_names)} tool(s) across {len(namespaces)} server(s)."
                ),
                config_source="in-process:clio_agent.tools.gateway",
                next_action="No action required.",
                capabilities=tool_names,
                details={"servers": namespaces},
                required=True,
            )
        return IntegrationStatus(
            name="gateway",
            state=IntegrationState.DEGRADED,
            summary="Gateway is reachable but exposes no tools.",
            config_source="in-process:clio_agent.tools.gateway",
            next_action="Mount at least one MCP server on the gateway.",
            capabilities=tool_names,
            details={"servers": namespaces},
            required=True,
        )

    def probe_data_backends(self, gateway_tools: set[str]) -> list[IntegrationStatus]:
        """Verify the Python backend of each data server actually mounted.

        This is structural grounding, not a universal requirement: we only
        report on a backend when the active gateway exposes tools in its
        namespace. A backend whose tools are not mounted is simply not part
        of this deployment and produces no status. When a server *is* mounted
        but its Python dependency cannot be imported, that mount is broken and
        is surfaced as UNAVAILABLE.
        """
        namespaces = {name.split("_", 1)[0] for name in gateway_tools if "_" in name}
        statuses: list[IntegrationStatus] = []
        for namespace, module_name in sorted(_DATA_BACKEND_MODULES.items()):
            if namespace not in namespaces:
                continue
            tools = sorted(tool for tool in gateway_tools if tool.split("_", 1)[0] == namespace)
            if not self.module_checker(module_name):
                statuses.append(
                    IntegrationStatus(
                        name=namespace,
                        state=IntegrationState.UNAVAILABLE,
                        summary=(
                            f"{namespace} tools are mounted but {module_name} is not importable."
                        ),
                        config_source=f"python import:{module_name}; in-process gateway",
                        next_action=(
                            f"Install the {namespace} runtime dependency with the project extras."
                        ),
                        capabilities=tools,
                        required=True,
                    )
                )
                continue
            statuses.append(
                IntegrationStatus(
                    name=namespace,
                    state=IntegrationState.READY,
                    summary=f"{namespace} backend and gateway tools are available.",
                    config_source=f"python import:{module_name}; in-process gateway",
                    next_action="No action required.",
                    capabilities=tools,
                    required=True,
                )
            )
        return statuses

    def probe_api(
        self,
        *,
        api_state: IntegrationState | str | None = None,
        api_error: str | None = None,
    ) -> IntegrationStatus:
        """Probe the gact ``/v1`` API surface (or report in-process app state).

        Live probing hits ``/v1/health`` and ``/v1/capabilities`` — the surface
        production actually serves — instead of the legacy ``/health /query
        /experts`` endpoints (#800), and reports the capability summary.
        """
        if api_state is not None:
            state = IntegrationState(api_state)
            details: dict[str, Any] = {}
            if state != IntegrationState.READY:
                details["reason"] = "api_startup_error"
                details["error"] = api_error or "unknown startup error"
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
                capabilities=list(_GACT_API_ENDPOINTS),
                details=details,
                required=True,
            )

        endpoint = self.env.get("CLIO_API_BASE")
        if not endpoint:
            return IntegrationStatus(
                name="api",
                state=IntegrationState.SKIPPED,
                summary="No API endpoint configured for live probing.",
                config_source="default:no CLIO_API_BASE",
                next_action=(
                    "Start the gact server (clio start) or set CLIO_API_BASE to its "
                    "base URL for live /v1 health checks."
                ),
                capabilities=list(_GACT_API_ENDPOINTS),
                required=True,
            )

        health_url = endpoint.rstrip("/") + "/v1/health"
        try:
            response = self.http_get(health_url, timeout=self.api_timeout)
        except Exception as exc:  # noqa: BLE001 - surfaced in IntegrationStatus (degraded doctor row)
            return IntegrationStatus(
                name="api",
                state=IntegrationState.UNAVAILABLE,
                summary=f"gact API is not reachable at {health_url}: {exc}",
                config_source="env:CLIO_API_BASE",
                next_action="Start the gact server (clio start) or correct CLIO_API_BASE.",
                endpoint=endpoint,
                capabilities=list(_GACT_API_ENDPOINTS),
                details={"reason": "gact_unreachable", "error": str(exc)},
                required=True,
            )

        status_code = getattr(response, "status_code", 200)
        overall_status = ""
        unhealthy: list[str] = []
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - unparseable body treated as None; reflected in status
            body = None
        if isinstance(body, dict):
            overall_status = str(body.get("overall_status", ""))
            rows = body.get("integrations")
            if isinstance(rows, list):
                unhealthy = [
                    str(row.get("name", "?"))
                    for row in rows
                    if isinstance(row, dict) and row.get("status") != "ready"
                ]

        if status_code >= 500 or overall_status in {"degraded", "unavailable"}:
            state = (
                IntegrationState.UNAVAILABLE
                if status_code >= 500 or overall_status == "unavailable"
                else IntegrationState.DEGRADED
            )
            return IntegrationStatus(
                name="api",
                state=state,
                summary=(f"gact /v1/health reports {overall_status or '?'} (HTTP {status_code})."),
                config_source="env:CLIO_API_BASE",
                next_action="Inspect the failing gact integrations and server logs.",
                endpoint=endpoint,
                capabilities=list(_GACT_API_ENDPOINTS),
                details={
                    "reason": "gact_unhealthy",
                    "http_status": status_code,
                    "health_status": overall_status,
                    "unhealthy_integrations": unhealthy,
                },
                required=True,
            )

        capabilities_url = endpoint.rstrip("/") + "/v1/capabilities"
        caps_body: Any = None
        caps_error = ""
        try:
            caps_response = self.http_get(capabilities_url, timeout=self.api_timeout)
            caps_status = getattr(caps_response, "status_code", 200)
            if caps_status >= 400:
                caps_error = f"HTTP {caps_status}"
            else:
                caps_body = caps_response.json()
        except Exception as exc:  # noqa: BLE001 - probe error captured in caps_error and surfaced
            caps_error = str(exc)
        if caps_error or not isinstance(caps_body, dict):
            return IntegrationStatus(
                name="api",
                state=IntegrationState.DEGRADED,
                summary=(
                    "gact /v1/health is ready but /v1/capabilities failed: "
                    f"{caps_error or 'malformed response'}."
                ),
                config_source="env:CLIO_API_BASE",
                next_action="Inspect the gact server logs; the capability catalog should be static.",
                endpoint=endpoint,
                capabilities=list(_GACT_API_ENDPOINTS),
                details={
                    "reason": "gact_capabilities_unavailable",
                    "http_status": status_code,
                    "health_status": overall_status,
                    "error": caps_error or "malformed response",
                },
                required=True,
            )

        flags = caps_body.get("capabilities")
        enabled = (
            sorted(name for name, value in flags.items() if value is True)
            if isinstance(flags, dict)
            else []
        )
        contract_version = str(caps_body.get("contract_version", ""))
        backend_info = caps_body.get("backend")
        return IntegrationStatus(
            name="api",
            state=IntegrationState.READY,
            summary=(
                f"gact /v1 API is ready: contract {contract_version or '?'}, "
                f"{len(enabled)} capabilities enabled."
            ),
            config_source="env:CLIO_API_BASE",
            next_action="No action required.",
            endpoint=endpoint,
            capabilities=enabled,
            details={
                "http_status": status_code,
                "health_status": overall_status or "ready",
                "contract_version": contract_version,
                "backend": backend_info if isinstance(backend_info, dict) else {},
                "capabilities_enabled": enabled,
            },
            required=True,
        )

    def probe_clio_core(self) -> IntegrationStatus:
        """Probe the production clio-core runtime: pip package + shared daemon.

        #800 retired the source-repo layout discovery (build/bin chimaera
        binaries, docker/quickstart YAML): it probed a deployment shape that no
        longer exists, so it stayed green while the real runtime was broken.
        The production runtime is the pip ``iowarp_core`` package plus the
        shared ``clio_run`` daemon — the same reality :meth:`probe_arc` gates
        on for the CTE backend (one shared helper, no duplication). The row is
        required exactly when the ARC backend is ``cte``.
        """
        backend, backend_source = self._arc_backend()
        required = backend == "cte"
        runtime = self._probe_cte_runtime()
        source = f"pip:iowarp_core; {backend_source}"
        endpoint = f"127.0.0.1:{runtime.port}"
        details = {"arc_backend": backend, **runtime.to_details()}

        if not runtime.installed:
            if not required:
                return IntegrationStatus(
                    name="clio_core",
                    state=IntegrationState.SKIPPED,
                    summary=(
                        "clio-core runtime (pip iowarp_core) is not installed; the ARC "
                        f"backend is {backend!r} so it is not required."
                    ),
                    config_source=source,
                    next_action=(
                        "Install the iowarp-core pip package and set CLIO_ARC_STORE=cte "
                        "to enable the clio-core runtime."
                    ),
                    details=details,
                    required=False,
                )
            return IntegrationStatus(
                name="clio_core",
                state=IntegrationState.UNAVAILABLE,
                summary=(
                    "clio-core runtime is required (ARC backend 'cte') but the "
                    "iowarp_core pip package is not installed."
                ),
                config_source=source,
                next_action=("Install the iowarp-core pip package, or set CLIO_ARC_STORE=local."),
                endpoint=endpoint,
                details=details,
                required=True,
            )

        if not runtime.daemon_alive:
            return IntegrationStatus(
                name="clio_core",
                state=IntegrationState.UNAVAILABLE,
                summary=(
                    "iowarp_core is installed but the shared clio-core daemon is not "
                    f"listening on port {runtime.port}."
                ),
                config_source=source,
                next_action=(
                    "Start the shared clio-core daemon (clio start / clio_run start); "
                    f"see {runtime.log_path}."
                ),
                endpoint=endpoint,
                details=details,
                required=required,
            )

        return IntegrationStatus(
            name="clio_core",
            state=IntegrationState.READY,
            summary=(
                "clio-core runtime is live: iowarp_core is installed and the shared "
                f"daemon is listening on port {runtime.port}."
            ),
            config_source=source,
            next_action="No action required.",
            endpoint=endpoint,
            capabilities=["pip-runtime", "shared-daemon"],
            details=details,
            required=required,
        )

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

        # Argonne: leave api_key blank in the LMProviderConfig the
        # probe constructs — it's only used to display config_source,
        # never to call out to the network here. The probe path itself
        # (_probe_argonne) reports separately on token presence.
        if provider == "argonne" and not api_key:
            api_key = ""
            key_source = "argonne:globus-deferred"

        config = LMProviderConfig(
            provider=provider,  # type: ignore[arg-type]
            api_base=api_base,
            model=model,
            api_key=api_key,
            temperature=self._float_env("CLIO_LM_TEMPERATURE", 0.0),
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
        except Exception as exc:
            raise ModelDiscoverySchemaError(
                f"invalid JSON from /models: {exc}",
                code="invalid_json",
            ) from exc
        if not isinstance(data, dict):
            raise ModelDiscoverySchemaError(
                "/models response was not a JSON object.",
                code="malformed_schema",
            )
        raw_models = data.get("data")
        if not isinstance(raw_models, list):
            raise ModelDiscoverySchemaError(
                "/models response missing data[] array.",
                code="malformed_schema",
            )
        models: list[str] = []
        malformed_items = 0
        for item in raw_models:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                model_id = item["id"].strip()
                if model_id:
                    models.append(model_id)
                else:
                    malformed_items += 1
            else:
                malformed_items += 1
        if raw_models and not models:
            raise ModelDiscoverySchemaError(
                f"/models response had {malformed_items} model row(s) but no usable id fields.",
                code="malformed_schema",
            )
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
        server = tool.name.split("_", 1)[0] if "_" in tool.name else tool.name
        capabilities.append(
            {
                "name": tool.name,
                "description": first_sentence,
                "server": server,
            }
        )
    return capabilities
