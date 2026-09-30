"""The cross-provider table of terminal provider signals (#1529).

ONE place lists every terminal, never-retry provider failure that survives
LiteLLM's generic exception re-wrap via its own CLIO-owned marker (see
:mod:`clio_agent.providers.terminal_signal` for the mechanism). Consumers
(the LM retry layer, the turn error-presentation layer) loop over this ONE
table instead of chaining a per-signal ``if contains_X(exc): ... elif
contains_Y(exc): ...``. Adding a new terminal signal means defining it in its
own provider module (the exception class, the detector that classifies the
SDK's structured/textual signal, and a :class:`TerminalProviderSignal`
constant) and adding that constant here -- not touching the LM retry layer or
the error-presentation layer at all.

Deliberately excludes :data:`clio_agent.providers.claude_code_errors
.CLAUDE_CODE_SIGNED_OUT_MESSAGE` / :data:`clio_agent.providers.codex.errors
.CODEX_AUTHENTICATION_ERROR_MESSAGE`: those are scoped to the turn's
CONFIGURED provider (``cli_provider_stream_failure`` / ``provider_auth_failure``
in ``gact/stream_failures.py``), because their detectors match generic HTTP
prose ("401"/"unauthorized") that could otherwise misattribute an unrelated
provider's auth failure. Every signal in this table is unconditional on the
configured provider by design -- its marker is unambiguous, and a plan-limit
or safety-refusal hit can legitimately surface after the session has since
moved to a different provider.
"""

from __future__ import annotations

from clio_agent.providers.claude_code_plan_limit import PLAN_LIMIT_SIGNAL
from clio_agent.providers.claude_code_safety_refusal import SAFETY_REFUSAL_SIGNAL
from clio_agent.providers.codex.errors import CODEX_PLAN_LIMIT_SIGNAL
from clio_agent.providers.terminal_signal import TerminalProviderSignal

__all__ = ["TERMINAL_PROVIDER_SIGNALS"]

#: Every terminal, provider-agnostic-to-detect signal registered so far.
#: Order matters only as a tie-break when a wrapped node's text could (in
#: theory) contain more than one marker -- the first match wins.
TERMINAL_PROVIDER_SIGNALS: tuple[TerminalProviderSignal, ...] = (
    PLAN_LIMIT_SIGNAL,
    SAFETY_REFUSAL_SIGNAL,
    CODEX_PLAN_LIMIT_SIGNAL,
)
