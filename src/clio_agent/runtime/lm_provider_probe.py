"""LM-provider doctor-probe helpers (owner module, iowarp/clio-agent#899).

The doctor's ``lm_provider`` row is built by
:meth:`clio_agent.runtime.status.RuntimeProbe.probe_lm_provider`. Two pieces of
that logic live here so ``status.py`` stays under its size ratchet and the
transport handling has a single owner:

* :func:`extract_models` / :class:`ModelDiscoverySchemaError` -- parse an
  OpenAI-compatible ``/models`` HTTP response (the HTTP-transport path).
* :func:`probe_cli_transport` -- the **transport-aware** probe for the CLI/SDK
  pseudo-schemes (``codex://sdk``, ``claude-code://sdk``). These providers have
  no HTTP ``/models`` endpoint; an HTTP GET against the pseudo-scheme yields
  ``requests``' ``No connection adapters were found`` and reports the provider
  UNAVAILABLE while turns actually run fine (#899). Claude's SDK transport
  requires both the optional ``claude_agent_sdk`` package and the local CLI;
  Codex owns a separate bundled-runtime probe. No pseudo-scheme is HTTP-probed.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from clio_agent.config import LMProviderConfig
from clio_agent.runtime.status import IntegrationState, IntegrationStatus


class ModelDiscoverySchemaError(ValueError):
    """Raised when an OpenAI-compatible /models response is malformed."""

    def __init__(self, message: str, *, code: str) -> None:
        self.code = code
        super().__init__(message)


def extract_models(response: Any) -> list[str]:
    """Extract model ids from an OpenAI-compatible ``/models`` response.

    Raises:
        ModelDiscoverySchemaError: The body is not JSON, is not an object, lacks a
            ``data[]`` array, or carries rows without a usable ``id``.
    """
    try:
        data = response.json()
    except Exception as exc:  # noqa: BLE001 - re-raised as a typed schema error below
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


# Provider id -> (CLI binary probed on PATH, human label). Both CLI/SDK
# transports ultimately spawn this local binary, so its presence on PATH is the
# meaningful readiness signal (the SDK client cannot run without it).
_CLI_TRANSPORT_BINARIES: dict[str, tuple[str, str]] = {
    "claude_code": ("claude", "Claude Code"),
}


def _codex_auth_path() -> Path:
    """Return the official SDK authentication file location."""
    configured = os.environ.get("CODEX_HOME", "").strip()
    return (Path(configured) if configured else Path.home() / ".codex") / "auth.json"


def _bundled_codex_path() -> Path | None:
    """Resolve the SDK-pinned Codex binary without consulting ``PATH``."""
    try:
        from codex_cli_bin import bundled_codex_path  # noqa: PLC0415

        return Path(bundled_codex_path())
    except (ImportError, OSError, RuntimeError):
        return None


def _probe_codex_sdk(
    config: LMProviderConfig,
    source: str,
    auth_mode: str,
) -> IntegrationStatus:
    """Probe the three dependencies the official Codex SDK actually consumes."""
    sdk_present = importlib.util.find_spec("openai_codex") is not None
    bundled_binary = _bundled_codex_path()
    auth_path = _codex_auth_path()
    details: dict[str, Any] = {
        "provider": "codex",
        "model": config.model,
        "transport": "sdk",
        "sdk_module": "openai_codex",
        "bundled_binary": str(bundled_binary or ""),
        "auth_path": str(auth_path),
    }
    missing: list[str] = []
    if not sdk_present:
        missing.append("sdk_module_absent")
    if bundled_binary is None or not bundled_binary.is_file():
        missing.append("bundled_binary_absent")
    if not auth_path.is_file():
        missing.append("auth_absent")
    if missing:
        return IntegrationStatus(
            name="lm_provider",
            state=IntegrationState.UNAVAILABLE,
            summary="Codex SDK transport is unavailable: " + ", ".join(missing) + ".",
            config_source=source,
            next_action="Install the Codex SDK extra and authenticate Codex on this machine.",
            endpoint=config.api_base,
            auth_mode=auth_mode,
            details={**details, "reason": missing[0], "missing": missing},
            required=True,
        )
    return IntegrationStatus(
        name="lm_provider",
        state=IntegrationState.READY,
        summary="Codex official Python SDK, bundled runtime, and authentication are ready.",
        config_source=source,
        next_action="No action required.",
        endpoint=config.api_base,
        auth_mode=auth_mode,
        capabilities=["chat-completions", "sdk-transport"],
        details=details,
        required=True,
    )


def _which_cli(binary: str) -> str | None:
    """Resolve a CLI binary on PATH, honouring the Windows ``.cmd`` launcher shim.

    Mirrors the resolution in the Claude Code / Codex LiteLLM providers so the
    doctor's readiness signal matches what the transport will actually spawn.
    """
    if os.name == "nt":
        cmd_path = shutil.which(f"{binary}.cmd")
        if cmd_path:
            return cmd_path
    return shutil.which(binary)


def is_http_transport(api_base: str) -> bool:
    """True when ``api_base`` is a real HTTP(S) endpoint (vs a CLI/SDK pseudo-scheme)."""
    return urlparse(api_base).scheme in ("http", "https")


def probe_cli_transport(
    config: LMProviderConfig,
    source: str,
    auth_mode: str,
    *,
    which: Callable[[str], str | None] = _which_cli,
) -> IntegrationStatus:
    """Transport-aware doctor probe for CLI/SDK pseudo-scheme providers (#899).

    The ``api_base`` (e.g. ``claude-code://sdk``, ``codex://sdk``) has no HTTP
    ``/models`` endpoint. This validates the local dependencies that the
    selected transport actually imports or starts rather than issuing an HTTP
    GET that would always report the provider unreachable.

    Args:
        config: The resolved provider config (provider, api_base, model).
        source: The doctor ``config_source`` label for the row.
        auth_mode: The auth-mode label for the row.
        which: Injectable CLI resolver ``(binary) -> path | None`` for testing.

    Returns:
        A READY row when the CLI is on PATH, else a typed UNAVAILABLE row naming
        the missing binary (``reason=cli_binary_absent``).
    """
    if config.provider == "codex":
        return _probe_codex_sdk(config, source, auth_mode)

    parsed = urlparse(config.api_base)
    transport = parsed.netloc or parsed.path.lstrip("/") or "cli"
    binary, label = _CLI_TRANSPORT_BINARIES.get(config.provider, (config.provider, config.provider))
    details: dict[str, Any] = {
        "provider": config.provider,
        "model": config.model,
        "transport": transport,
        "cli_binary": binary,
    }

    if config.provider == "claude_code" and importlib.util.find_spec("claude_agent_sdk") is None:
        return IntegrationStatus(
            name="lm_provider",
            state=IntegrationState.UNAVAILABLE,
            summary=(
                "Claude Code sdk transport selected but the `claude_agent_sdk` package "
                "is not installed; the provider cannot start its SDK transport."
            ),
            config_source=source,
            next_action="Install the Claude transport with `uv sync --extra claude-code`.",
            endpoint=config.api_base,
            auth_mode=auth_mode,
            details={**details, "reason": "sdk_package_absent", "sdk_package": "claude_agent_sdk"},
            required=True,
        )

    resolved = which(binary)
    if resolved:
        return IntegrationStatus(
            name="lm_provider",
            state=IntegrationState.READY,
            summary=(
                f"{label} {transport} transport is ready: `{binary}` CLI found on PATH "
                "(no HTTP /models endpoint — probed the CLI the transport spawns)."
            ),
            config_source=source,
            next_action="No action required.",
            endpoint=config.api_base,
            auth_mode=auth_mode,
            capabilities=["chat-completions", f"{transport}-transport"],
            details={**details, "cli_path": resolved},
            required=True,
        )
    return IntegrationStatus(
        name="lm_provider",
        state=IntegrationState.UNAVAILABLE,
        summary=(
            f"{label} {transport} transport selected but the `{binary}` CLI is not on "
            "PATH; the provider cannot spawn its transport."
        ),
        config_source=source,
        next_action=(
            f"Install {label} and run `{binary} login` once per machine, or set "
            "CLIO_LM_PROVIDER/CLIO_LM_API_BASE to a reachable HTTP provider."
        ),
        endpoint=config.api_base,
        auth_mode=auth_mode,
        details={**details, "reason": "cli_binary_absent"},
        required=True,
    )
