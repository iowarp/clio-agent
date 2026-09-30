"""Typed classification for a Claude Code safety-filter refusal (#1529 follow-up).

Live evidence (a GACT session export): a turn's SDK ``ResultMessage`` failed
with ``result`` text naming Anthropic's own canned safety-refusal template --
"Sonnet 5.5's safeguards flagged this message
(https://www.anthropic.com/legal/aup). This sometimes happens with safe,
normal conversations. Claude Code can't respond to this message with Sonnet
5.5. Try rephrasing the request in a new session or change your model. ...
Request ID: req_... Message ID: msg_..." -- with NO dedicated
``api_error_status`` the way the 401/404/429 signals in
``claude_code_result_errors.py`` carry one. Detection therefore matches
Anthropic's own STABLE canned wording (the fixed apology template every
safety-filter refusal uses, not the model's own conversational output) the
same way ``codex.errors.is_usage_limit_text`` classifies a Codex backend's
usage-limit wording from its own free-text body -- never a heuristic over
what the model said.
"""

from __future__ import annotations

import re
from typing import Any

from clio_agent.providers.terminal_signal import TerminalProviderSignal

__all__ = [
    "SAFETY_REFUSAL_MARKER",
    "SAFETY_REFUSAL_SIGNAL",
    "ClaudeCodeSafetyRefusalError",
    "is_safety_refusal_text",
    "safety_refusal_from_result",
]

#: Anthropic's own stable canned wording for a Claude Code safety-filter
#: refusal (the AUP safeguards apology template) -- a fixed string Anthropic
#: ships, not conversational prose the model generated, so matching it here
#: classifies a STRUCTURED SDK result exactly the way
#: ``codex.errors.is_usage_limit_text`` classifies a Codex backend's own
#: usage-limit wording.
_DETECTION_MARKERS = ("safeguards flagged this message",)

#: CLIO's own stable marker, the start of every message this module raises.
#: Survives LiteLLM's traceback-embedding re-wrap the same way
#: ``claude_code_plan_limit.PLAN_LIMIT_MESSAGE_MARKER`` does -- see that
#: module's docstring for the full mechanism (BerriAI/litellm#4201).
SAFETY_REFUSAL_MARKER = "Claude declined this request"

#: A CLIO-owned recovery token embedded BEFORE the marker so the provider's
#: request id survives LiteLLM's re-wrap the same way the marker text does,
#: without appearing in the clean, user-facing sentence
#: :func:`~clio_agent.providers.terminal_signal.recover_message` slices out
#: (which starts AT the marker, skipping this prefix).
_REQUEST_ID_TOKEN_RE = re.compile(r"\[cc_request_id=(?P<id>[^\]]*)\]")

#: Anthropic's own "Request ID: req_..." / "Message ID: msg_..." lines in the
#: CLI's raw result text (read BEFORE any LiteLLM wrapping, at the original
#: raise site -- never re-derived from the wrapped text downstream).
_CLI_REQUEST_ID_RE = re.compile(r"Request ID:\s*(?P<id>\S+)")
_CLI_MESSAGE_ID_RE = re.compile(r"Message ID:\s*(?P<id>\S+)")


def is_safety_refusal_text(text: str) -> bool:
    """Whether ``text`` (a ``ResultMessage``'s own result) names a safety-filter refusal."""
    lowered = (text or "").casefold()
    return any(marker in lowered for marker in _DETECTION_MARKERS)


class ClaudeCodeSafetyRefusalError(RuntimeError):
    """Anthropic's safety filter refused the request -- terminal, never retried.

    Rephrasing the SAME message will not pass the same filter until the user
    changes it (or moves to a new session/model), so this must surface as a
    clear, terminal, user-facing message rather than being silently retried
    (same rationale as ``ClaudeCodePlanLimitError``).
    """

    def __init__(
        self,
        *,
        detail: str,
        model: str | None = None,
        request_id: str | None = None,
        message_id: str | None = None,
    ) -> None:
        # The request-id token rides BEFORE the marker (see its docstring) so
        # it survives LiteLLM's re-wrap without polluting the clean sentence.
        token = f"[cc_request_id={request_id}] " if request_id else ""
        super().__init__(
            f"{token}{SAFETY_REFUSAL_MARKER} (its safety filter flagged it, which can "
            "happen with normal requests). Rephrase it in a new session or switch the model."
        )
        #: Anthropic's own words (kept verbatim, unprefixed).
        self.detail = detail
        #: The claude_code model id the failing call used, or ``None``.
        self.model = model
        #: The provider's own request id (``Request ID: req_...``), or ``None``.
        self.request_id = request_id
        #: The provider's own message id (``Message ID: msg_...``), or ``None``.
        self.message_id = message_id


def safety_refusal_from_result(
    result_message: Any, *, model: str | None = None
) -> ClaudeCodeSafetyRefusalError | None:
    """Classify a terminal ``ResultMessage`` -- ``None`` unless its text names a safety refusal.

    Args:
        result_message: A ``claude_agent_sdk.types.ResultMessage`` (duck-typed).
        model: The claude_code model id the call used, when the caller has it.

    Returns:
        A typed :class:`ClaudeCodeSafetyRefusalError` when ``is_error`` and the
        CLI's own result text names a safety-filter refusal, else ``None``.
    """
    if not getattr(result_message, "is_error", False):
        return None
    text = str(getattr(result_message, "result", "") or "")
    if not is_safety_refusal_text(text):
        return None
    request_id = _CLI_REQUEST_ID_RE.search(text)
    message_id = _CLI_MESSAGE_ID_RE.search(text)
    return ClaudeCodeSafetyRefusalError(
        detail=" ".join(text.split()),
        model=model,
        request_id=request_id.group("id") if request_id else None,
        message_id=message_id.group("id") if message_id else None,
    )


def _safety_refusal_extra_details(node: Any) -> dict[str, Any]:
    """``model``/``request_id``/``message_id`` for the registry's ``extra_details``.

    Full fidelity when ``node`` is the live :class:`ClaudeCodeSafetyRefusalError`
    (not guaranteed once LiteLLM has re-wrapped it); the request id alone,
    recovered from the ``[cc_request_id=...]`` token LiteLLM's re-wrap still
    carries as a literal substring, otherwise.
    """
    if isinstance(node, ClaudeCodeSafetyRefusalError):
        return {
            "model": node.model,
            "request_id": node.request_id,
            "message_id": node.message_id,
        }
    match = _REQUEST_ID_TOKEN_RE.search(str(node))
    return {"model": None, "request_id": match.group("id") if match else None, "message_id": None}


#: This signal's registry entry (see
#: :mod:`clio_agent.providers.terminal_signal_catalog`).
SAFETY_REFUSAL_SIGNAL = TerminalProviderSignal(
    reason="provider_safety_refusal",
    marker=SAFETY_REFUSAL_MARKER,
    provider_id="claude_code",
    exception_type=ClaudeCodeSafetyRefusalError,
    recovery_actions=("switch_model", "retry"),
    extra_details=_safety_refusal_extra_details,
)
