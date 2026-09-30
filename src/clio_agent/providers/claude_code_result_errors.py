"""Classify a failed Claude Agent SDK result into CLIO's typed provider errors.

The SDK ends a failed turn with a ``ResultMessage`` whose ``is_error`` is true.
Its ``subtype`` describes the agent LOOP, not the failure: a turn whose only
API call was refused still reports ``subtype="success"`` (#1454, verified live
against claude-agent-sdk 0.2.156 with a signed-out ``CLAUDE_CONFIG_DIR``)::

    AssistantMessage(content=[TextBlock("Not logged in · Please run /login")],
                     model="<synthetic>", error="authentication_failed")
    ResultMessage(subtype="success", is_error=True, api_error_status=None,
                  result="Not logged in · Please run /login",
                  terminal_reason="api_error")

The old raise reported ``api_error_status or subtype``, so a signed-out user
read "claude agent sdk returned an error for model=...: success" and the CLI's
own explanation was dropped. This owner module reads the STRUCTURED signals
instead (never prose): ``AssistantMessage.error`` (the SDK's typed
``AssistantMessageError``) and ``ResultMessage.api_error_status``.
"""

from __future__ import annotations

from typing import Any

from clio_agent.providers._cli_provider import raise_model_rejected
from clio_agent.providers.claude_code_errors import ClaudeCodeSignedOutError
from clio_agent.providers.claude_code_plan_limit import plan_limit_from_result
from clio_agent.providers.claude_code_safety_refusal import safety_refusal_from_result

__all__ = [
    "CLAUDE_CODE_AUTH_FAILED",
    "CLAUDE_CODE_AUTH_STATUS",
    "CLAUDE_CODE_REJECTION_STATUS",
    "raise_classified_result_error",
    "result_error_detail",
]

#: A ``ResultMessage.api_error_status`` of 404 is the ONLY definitive
#: model-rejection signal claude_code exposes (#1184, #1211 review A3/D3). Any
#: other ``is_error`` status (429/5xx/None) stays off the rejection path --
#: transient noise must never be misclassified as a rejection.
CLAUDE_CODE_REJECTION_STATUS = 404

#: The SDK's typed ``AssistantMessageError`` value for a refused credential
#: (signed out, expired or revoked sign-in).
CLAUDE_CODE_AUTH_FAILED = "authentication_failed"

#: The HTTP status the API answers a refused credential with.
CLAUDE_CODE_AUTH_STATUS = 401


def result_error_detail(msg: Any) -> str:
    """The most specific description a failed ``ResultMessage`` carries.

    The CLI's own ``result`` text first (it names the cause), then the API
    status, then the subtype -- which alone says nothing about the failure.
    """

    text = " ".join(str(getattr(msg, "result", "") or "").split())
    if text:
        return text
    status = getattr(msg, "api_error_status", None)
    return str(status or getattr(msg, "subtype", None) or "unknown error")


def raise_classified_result_error(msg: Any, *, model: str, assistant_error: str | None) -> None:
    """Raise the typed error for a classified failed result; return otherwise.

    Args:
        msg: The failed ``ResultMessage`` (duck-typed so tests need no SDK).
        model: The claude_code model id the call used.
        assistant_error: The ``AssistantMessage.error`` the same call reported,
            or ``None``.

    Raises:
        ClaudeCodeSignedOutError: The credential was refused (typed signal).
        litellm.BadRequestError: The model was rejected (404).
        ClaudeCodePlanLimitError: The plan window is exhausted (429).
        ClaudeCodeSafetyRefusalError: Anthropic's safety filter refused the
            request (#1529 follow-up; no dedicated status code, so this is
            the last check, after every status-coded signal above).
    """

    status = getattr(msg, "api_error_status", None)
    if assistant_error == CLAUDE_CODE_AUTH_FAILED or status == CLAUDE_CODE_AUTH_STATUS:
        raise ClaudeCodeSignedOutError(detail=result_error_detail(msg), model=model)
    if status == CLAUDE_CODE_REJECTION_STATUS:
        # A definitive rejection (verified live: api_error_status 404 + an
        # "issue with the selected model" result text) -- never retried as
        # transient, and the CLI's own text rides into the transcript.
        raise_model_rejected(
            message=(
                f"claude_code rejected model {model!r} (api_error_status={status}): "
                f"{getattr(msg, 'result', '') or 'model not available'}"
            ),
            model=f"claude_code/{model}",
            llm_provider="claude_code",
        )
    plan_limit = plan_limit_from_result(msg, model=model)
    if plan_limit is not None:
        raise plan_limit
    safety_refusal = safety_refusal_from_result(msg, model=model)
    if safety_refusal is not None:
        raise safety_refusal
