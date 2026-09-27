"""Claude Code model discovery: the maintained catalog plus one CLI sign-in check.

Per owner ruling, Claude Code model existence, per-model input-modality
capabilities, and the account default come ONLY from the trusted GitHub
catalog document (:mod:`.claude_code_catalog`), acquired at startup and on
explicit provider verification -- exactly like Codex's model list is acquired
from the official SDK. CLIO does not probe models through the SDK/CLI to learn
what exists or what a model can do: there are no per-model probes, no bare-
default probe, and no multimodal probe. The one exception is per-model effort
levels, which only the CLI knows: :mod:`.claude_code_effort` reads them from the
CLI's ``initialize`` model list (no model turn) and annotates the catalog rows.

The catalog is silent on whether Claude Code is INSTALLED or SIGNED IN on this
machine, so discovery separately runs exactly one small check: ``<binary> auth
status``. The Claude Code CLI prints JSON like ``{"loggedIn": true,
"authMethod": "claude.ai", "apiProvider": "firstParty", ...}`` (verified on CLI
2.1.276 and 2.1.280); ``loggedIn is True`` is the only signal this module
trusts. The binary is resolved by :func:`_resolve_claude_binary`, the SAME
selection the SDK transport pins as ``cli_path`` (installed vs bundled,
:mod:`clio_agent.providers.components.client_binary`), so this check never
validates a different binary than the one that actually runs.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any

from clio_agent import conf
from clio_agent.providers.model_discovery import claude_code_effort
from clio_agent.providers.model_discovery.claude_code_catalog import (
    ClaudeCodeCatalog,
    ClaudeCodeCatalogError,
    refresh_claude_code_catalog,
)
from clio_agent.providers.model_discovery.modality_evidence import modality_evidence
from clio_agent.providers.model_discovery.overlay import (
    CLAUDE_CODE_SOURCE,
    ProviderDiscoveryResult,
    attach_context_limits,
)

#: Seconds the ``<binary> auth status`` sign-in check may run before it is
#: abandoned as inconclusive.
#:
#: Configuration, not a compiled-in constant: a slow host, or a Claude Code CLI
#: cold-starting its own credential check, is a property of the operator's
#: machine, so an install that needs longer raises
#: ``providers.claude_code.auth_status_timeout_s`` /
#: ``CLIO_CLAUDE_CODE_AUTH_STATUS_TIMEOUT_S`` rather than patching this file.
#: Resolved at import (like ``gact/runtime/constants.py``'s ``_CTX_MAX_BYTES``)
#: because it is also this module's public default argument.
CLAUDE_CODE_AUTH_STATUS_TIMEOUT_S: float = conf.resolve(
    "providers.claude_code.auth_status_timeout_s",
    env="CLIO_CLAUDE_CODE_AUTH_STATUS_TIMEOUT_S",
    default=20.0,
    cast=conf.as_float,
)


class ClaudeCodeCLIUnavailableError(RuntimeError):
    """Raised when the ``claude`` binary isn't on PATH at discovery time."""


def _resolve_claude_binary() -> str:
    """Return the ``claude`` binary the SDK transport runs, or raise.

    The SAME selection the transport pins as ``cli_path``
    (:func:`~clio_agent.providers.components.client_binary.claude_client`), so
    the sign-in check asks the very CLI a turn will run -- never a different
    one found first on PATH.
    """
    from clio_agent.providers.components.client_binary import claude_client  # noqa: PLC0415

    path = claude_client().path
    if not path:
        raise ClaudeCodeCLIUnavailableError(
            "Claude Code runtime is unavailable. Install Claude Code support and sign in "
            "once on the connected agent."
        )
    return path


def _auth_status(binary: str, *, timeout: float) -> tuple[bool, str]:
    """Run ``<binary> auth status`` exactly once; return ``(signed_in, failed_reason)``.

    Never raises. This is the ONLY sign-in signal this module trusts:
    ``signed_in`` is True exactly when the CLI's own JSON reply sets
    ``loggedIn: true``. Every other outcome -- a timeout, a launch failure,
    non-JSON output, or ``loggedIn`` false/absent -- is a typed
    ``failed_reason`` with ``signed_in=False``. This function makes NO claim
    about which models exist or what they can do; that is the catalog's job.
    """
    args = [binary, "auth", "status"]
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell, no user-controlled input
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            **kwargs,
        )
    except subprocess.TimeoutExpired:
        return False, f"Claude Code auth status check timed out after {timeout}s"
    except OSError as exc:
        return False, f"Claude Code auth status check failed to launch: {exc}"
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        detail = (proc.stdout or proc.stderr or "")[:200]
        return False, f"Claude Code auth status returned non-JSON output: {detail!r}"
    if not isinstance(payload, dict) or payload.get("loggedIn") is not True:
        # CLIO drives the CLI's own sign-in (Log in), so the sentence names that.
        return False, (
            "Claude Code is not signed in on the connected agent. "
            "Sign in to Claude Code from CLIO to use its models."
        )
    return True, ""


def _explicit_catalog(candidates: tuple[str, ...]) -> ClaudeCodeCatalog:
    """Build a catalog stand-in for an explicit id list (diagnostic callers only).

    No capability can be claimed for an id that bypassed the maintained
    catalog, so each row carries text-only capabilities with a typed
    unevidenced marker -- never a guess.
    """
    return ClaudeCodeCatalog(
        models=[
            {
                "id": item,
                "name": item,
                "capabilities": ["text"],
                "capability_evidence": modality_evidence(
                    source="claude_code_catalog",
                    reason="modality_uncataloged",
                    unevidenced=("image", "pdf"),
                ),
            }
            for item in candidates
        ],
        default_model="",
        default_model_reason="",
    )


def discover_claude_code(
    *,
    candidates: tuple[str, ...] | None = None,
    timeout: float = CLAUDE_CODE_AUTH_STATUS_TIMEOUT_S,
) -> ProviderDiscoveryResult:
    """Trust the maintained catalog for models; verify sign-in with one CLI call.

    Claude Code offers no account model-enumeration endpoint, and per owner
    ruling CLIO does not manufacture one via per-model probing: the maintained
    GitHub catalog (:mod:`.claude_code_catalog`) is the single source of model
    ids, their input-modality capabilities, and the account default -- the same
    trust model as Codex's SDK-reported catalog. This function's only live
    check is whether Claude Code is installed and signed in on this machine
    (:func:`_resolve_claude_binary` + one ``auth status`` call); a transient
    catalog or CLI failure returns a typed failure and never promotes a cached
    list to current availability.
    """
    if candidates is None:
        try:
            catalog = refresh_claude_code_catalog()
        except ClaudeCodeCatalogError as exc:
            return ProviderDiscoveryResult(
                provider="claude_code",
                discovered=[],
                source=CLAUDE_CODE_SOURCE,
                failed_reason=str(exc),
            )
    else:
        # An explicit list is used by diagnostic callers and bounded live tests.
        catalog = _explicit_catalog(candidates)

    try:
        binary = _resolve_claude_binary()
    except ClaudeCodeCLIUnavailableError as exc:
        return ProviderDiscoveryResult(
            provider="claude_code", discovered=[], source=CLAUDE_CODE_SOURCE, failed_reason=str(exc)
        )

    signed_in, failed_reason = _auth_status(binary, timeout=timeout)
    if not signed_in:
        return ProviderDiscoveryResult(
            provider="claude_code",
            discovered=[],
            source=CLAUDE_CODE_SOURCE,
            failed_reason=failed_reason,
        )

    # Per-model effort levels come from the CLI's own model catalog (one SDK
    # initialize read, no model turn); the maintained catalog still decides
    # which models exist.
    cli_models, effort_failure = claude_code_effort.read_cli_model_catalog()
    rows = claude_code_effort.attach_effort_levels(
        [dict(model) for model in catalog.models], cli_models, effort_failure
    )
    discovered = attach_context_limits(rows, "claude_code")
    return ProviderDiscoveryResult(
        provider="claude_code",
        discovered=discovered,
        source=CLAUDE_CODE_SOURCE,
        default_model=catalog.default_model,
        default_model_reason=catalog.default_model_reason,
    )


__all__ = [
    "CLAUDE_CODE_AUTH_STATUS_TIMEOUT_S",
    "ClaudeCodeCLIUnavailableError",
    "discover_claude_code",
]
