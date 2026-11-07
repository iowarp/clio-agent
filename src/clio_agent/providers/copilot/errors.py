"""Stable, user-facing GitHub Copilot errors and the ``session.error`` classifier.

Every failure the Copilot runtime reports becomes one typed exception with a
stable ``reason``. The provider's own words stay in ``detail``; the line the
user sees is CLIO's constant, so it survives LiteLLM re-wrapping the exception
as text (the same marker mechanism as Codex and Claude Code plan limits).
"""

from __future__ import annotations

from typing import Any

from clio_agent.providers.terminal_signal import (
    TerminalProviderSignal,
    find_terminal_signal,
    recover_message,
)

COPILOT_NOT_INSTALLED_MESSAGE = "GitHub Copilot support is not installed on the connected agent."
COPILOT_INSTALL_FAILED_MESSAGE = (
    "CLIO could not install GitHub Copilot support. Check the connected agent's "
    "internet connection and try again."
)
COPILOT_RUNTIME_MISSING_MESSAGE = (
    "The GitHub Copilot runtime is not staged on the connected agent. "
    "Install GitHub Copilot support from Settings > Providers."
)
#: The one line a signed-out or refused GitHub sign-in shows the user.
COPILOT_SIGNED_OUT_MESSAGE = (
    "GitHub Copilot needs a GitHub sign-in. Sign in to GitHub in CLIO, then resend."
)
COPILOT_NO_SEAT_MESSAGE = (
    "The signed-in GitHub account has no GitHub Copilot access. "
    "Sign in with an account that has a Copilot plan."
)
COPILOT_POLICY_DISABLED_MESSAGE = (
    "This model is disabled by the GitHub Copilot policy of the signed-in account."
)
COPILOT_MODEL_UNAVAILABLE_MESSAGE = "This GitHub Copilot model is not available to this account."
#: CLIO's own marker for a premium-request limit; see :data:`COPILOT_PLAN_LIMIT_SIGNAL`.
COPILOT_PLAN_LIMIT_MESSAGE_MARKER = "GitHub Copilot premium request limit reached"


class CopilotError(RuntimeError):
    """Base class for every typed GitHub Copilot failure."""

    reason = "copilot_error"

    def __init__(
        self,
        message: str,
        *,
        detail: str = "",
        status_code: int | None = None,
        error_code: str | None = None,
    ) -> None:
        super().__init__(f"{message} ({detail})" if detail else message)
        #: The runtime's own words, verbatim.
        self.detail = detail
        self.status_code = status_code
        self.error_code = error_code


class CopilotRuntimeMissingError(CopilotError):
    """No Copilot runtime is staged or installed; a turn never downloads one."""

    reason = "copilot_runtime_missing"


class CopilotNotInstalledError(CopilotRuntimeMissingError):
    """The ``copilot`` extra (``github-copilot-sdk``) is not installed."""

    reason = "copilot_not_installed"


class CopilotAuthRequiredError(CopilotError):
    """GitHub is signed out in CLIO, or the Copilot service refused the token."""

    reason = "copilot_auth_required"


class CopilotNoSeatError(CopilotError):
    """The signed-in account has no Copilot entitlement."""

    reason = "copilot_no_seat"


class CopilotPolicyDisabledError(CopilotError):
    """The account's Copilot policy disables the requested model."""

    reason = "copilot_policy_disabled"


class CopilotModelUnavailableError(CopilotError):
    """The requested model is not in the account's live model list."""

    reason = "copilot_model_unavailable"


class CopilotSessionError(CopilotError):
    """A ``session.error`` CLIO has no more specific type for; carries the runtime's words."""

    reason = "copilot_session_error"


class CopilotPlanLimitError(CopilotError):
    """The account's premium-request allowance is exhausted -- terminal, never retried."""

    reason = "copilot_plan_limit"

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        resets_at: str | None = None,
    ) -> None:
        super().__init__(
            COPILOT_PLAN_LIMIT_MESSAGE_MARKER,
            detail=detail,
            status_code=status_code,
            error_code=error_code,
        )
        #: When the monthly allowance resets (ISO date), when the account quota said.
        self.resets_at = resets_at


def _plan_limit_extra_details(node: object) -> dict[str, object]:
    if isinstance(node, CopilotPlanLimitError):
        return {
            "error_code": node.error_code,
            "status_code": node.status_code,
            "resets_at": node.resets_at,
        }
    return {}


#: This signal's entry in :mod:`clio_agent.providers.terminal_signal_catalog`.
COPILOT_PLAN_LIMIT_SIGNAL = TerminalProviderSignal(
    reason="copilot_plan_limit",
    marker=COPILOT_PLAN_LIMIT_MESSAGE_MARKER,
    provider_id="copilot",
    exception_type=CopilotPlanLimitError,
    recovery_actions=("switch_model", "retry"),
    extra_details=_plan_limit_extra_details,
)


def copilot_plan_limit_message(value: object) -> str | None:
    """The clean one-line message for a Copilot plan-limit failure, or ``None``."""
    found = find_terminal_signal(value, (COPILOT_PLAN_LIMIT_SIGNAL,))
    if found is None:
        return None
    return recover_message(found[1], COPILOT_PLAN_LIMIT_MESSAGE_MARKER)


def contains_copilot_auth_required(value: Any) -> bool:
    """Whether an exception (or group, or message) carries a Copilot sign-in refusal.

    Matches CLIO's own :data:`COPILOT_SIGNED_OUT_MESSAGE`, never provider prose,
    walking exception groups and explicit causes.
    """
    if isinstance(value, CopilotAuthRequiredError) or COPILOT_SIGNED_OUT_MESSAGE in str(value):
        return True
    children = list(getattr(value, "exceptions", None) or ())
    cause = getattr(value, "__cause__", None)
    if cause is not None:
        children.append(cause)
    return any(contains_copilot_auth_required(child) for child in children)


def contains_copilot_dependency_error(value: Any) -> bool:
    """Whether an exception or message names missing Copilot support or runtime."""
    if isinstance(value, CopilotRuntimeMissingError):
        return True
    text = str(value)
    markers = (
        COPILOT_NOT_INSTALLED_MESSAGE,
        COPILOT_RUNTIME_MISSING_MESSAGE,
        "No module named 'copilot'",
    )
    if any(marker in text for marker in markers):
        return True
    return any(
        contains_copilot_dependency_error(child)
        for child in (getattr(value, "exceptions", None) or ())
    )


def _remediation(data: Any) -> str:
    value = getattr(data, "remediation", None)
    return str(getattr(value, "value", value) or "")


def classify_session_error(data: Any, *, resets_at: str | None = None) -> CopilotError:
    """Map one ``session.error`` payload to its typed CLIO error.

    Classification reads only the runtime's typed fields (``remediation``,
    ``status_code``, ``error_type``, ``error_code``); the free-text message is
    carried verbatim as ``detail`` and never parsed.
    """
    detail = str(getattr(data, "message", "") or "")
    status = getattr(data, "status_code", None)
    code = getattr(data, "error_code", None)
    kind = str(getattr(data, "error_type", "") or "").lower()
    code_text = str(code or "").lower()
    remediation = _remediation(data)
    if remediation == "sign_in" or status == 401 or kind in {"authentication", "auth"}:
        return CopilotAuthRequiredError(
            COPILOT_SIGNED_OUT_MESSAGE, detail=detail, status_code=status, error_code=code
        )
    if remediation == "switch_account" or code_text in {"no_copilot_access", "copilot_no_seat"}:
        return CopilotNoSeatError(
            COPILOT_NO_SEAT_MESSAGE, detail=detail, status_code=status, error_code=code
        )
    if kind == "quota" or status == 402 or "quota" in code_text:
        return CopilotPlanLimitError(
            detail, status_code=status, error_code=code, resets_at=resets_at
        )
    if "policy" in code_text or "policy" in kind:
        return CopilotPolicyDisabledError(
            COPILOT_POLICY_DISABLED_MESSAGE, detail=detail, status_code=status, error_code=code
        )
    if "model_not" in code_text or kind == "model_not_available":
        return CopilotModelUnavailableError(
            COPILOT_MODEL_UNAVAILABLE_MESSAGE, detail=detail, status_code=status, error_code=code
        )
    return CopilotSessionError(
        f"GitHub Copilot failed: {kind or 'error'}",
        detail=detail,
        status_code=status,
        error_code=code,
    )


def sdk_transport_errors() -> tuple[type[BaseException], ...]:
    """The SDK's JSON-RPC, process-exit and stop errors (empty without the extra)."""
    try:
        from copilot._jsonrpc import JsonRpcError, ProcessExitedError  # noqa: PLC0415
        from copilot.client import StopError  # noqa: PLC0415
    except ImportError:
        return ()
    return (JsonRpcError, ProcessExitedError, StopError)


def teardown_errors() -> tuple[type[BaseException], ...]:
    """What talking to a runtime can raise outside a turn: logged or recorded, never raised."""
    return (OSError, EOFError, RuntimeError, TimeoutError, *sdk_transport_errors())


__all__ = [
    "COPILOT_INSTALL_FAILED_MESSAGE",
    "COPILOT_MODEL_UNAVAILABLE_MESSAGE",
    "COPILOT_NOT_INSTALLED_MESSAGE",
    "COPILOT_NO_SEAT_MESSAGE",
    "COPILOT_PLAN_LIMIT_MESSAGE_MARKER",
    "COPILOT_PLAN_LIMIT_SIGNAL",
    "COPILOT_POLICY_DISABLED_MESSAGE",
    "COPILOT_RUNTIME_MISSING_MESSAGE",
    "COPILOT_SIGNED_OUT_MESSAGE",
    "CopilotAuthRequiredError",
    "CopilotError",
    "CopilotModelUnavailableError",
    "CopilotNoSeatError",
    "CopilotNotInstalledError",
    "CopilotPlanLimitError",
    "CopilotPolicyDisabledError",
    "CopilotRuntimeMissingError",
    "CopilotSessionError",
    "classify_session_error",
    "contains_copilot_auth_required",
    "contains_copilot_dependency_error",
    "copilot_plan_limit_message",
    "sdk_transport_errors",
    "teardown_errors",
]
