"""Shared plumbing for a CLIO-typed provider failure that survives LiteLLM's
generic exception re-wrap (#1529).

Both Claude Code's and Codex's ``astreaming`` re-raise their own typed
terminal exceptions (a plan-limit hit, a safety-filter refusal, ...) bare into
LiteLLM's ``CustomStreamWrapper``. LiteLLM does not recognize a CLIO-defined
exception type, so its generic exception-mapping fallback (its own documented
behavior for an unmapped exception, BerriAI/litellm#4201) re-wraps it as
``litellm.MidStreamFallbackError: litellm.APIConnectionError: <original>\\n
Traceback (most recent call last): ...`` -- CLIO cannot change that
third-party wrapping. Two consequences, both fixed ONCE here instead of once
per exception type:

1. Both wrapper class names are themselves entries in
   ``lm.io_logging._TRANSIENT_PROVIDER_MARKERS``, so the LM retry layer would
   silently retry what is actually a terminal failure.
2. The wrapped text dumps a raw traceback into the user-facing card instead
   of the clean sentence CLIO raised.

Every terminal provider failure that goes through a ``CustomLLM`` streaming
transport embeds a STABLE, CLIO-owned marker string at the start of its
message (never the provider's own prose directly, which is not guaranteed
stable -- see each signal's own module for how it derives a stable marker
from a structured field or the provider's own canned wording). LiteLLM's
re-wrap format keeps the original exception's ``str()`` as a literal
substring, so that marker survives verbatim -- recovering the clean sentence,
or excluding the failure from the transient-retry classifier, is then the
SAME tree-walk-and-match regardless of which terminal signal it is.

A new terminal signal registers ONE :class:`TerminalProviderSignal` (owned by
its own provider module, alongside the exception class and the detector that
classifies the SDK's structured/textual signal into it) in
:data:`clio_agent.providers.terminal_signal_catalog.TERMINAL_PROVIDER_SIGNALS`
instead of duplicating the exception-tree walk / marker-prefix /
retry-exclusion / error-info wiring per case.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "TRACEBACK_MARKER",
    "TerminalProviderSignal",
    "exception_tree",
    "find_terminal_signal",
    "is_terminal_provider_error",
    "recover_message",
]

#: LiteLLM appends the full traceback after this literal line when it cannot
#: map an exception type (BerriAI/litellm#4201, see the module docstring); a
#: recovered clean sentence ends right before it.
TRACEBACK_MARKER = "Traceback (most recent call last):"


def exception_tree(value: Any) -> list[Any]:
    """``value`` and every node reachable from it via ``.exceptions``/``__cause__``.

    An ``ExceptionGroup``'s own ``str()`` is just the opaque "unhandled errors
    in a TaskGroup (N sub-exceptions)" wrapper -- the real text lives on its
    ``.exceptions`` leaves -- so a marker search that only looked at
    ``str(value)`` would miss a marker one level down. Deliberately does NOT
    walk ``__context__`` (the implicit chain LiteLLM's re-raise leaves behind
    is not a guaranteed-stable mechanism across LiteLLM versions the way the
    marker text is): a text match against the (possibly wrapped) node is the
    reliable signal, the same convention ``claude_code_errors
    .contains_claude_code_signed_out`` already established.
    """
    seen: set[int] = set()
    pending = [value]
    nodes: list[Any] = []
    while pending:
        node = pending.pop(0)
        if id(node) in seen:
            continue
        seen.add(id(node))
        nodes.append(node)
        pending.extend(getattr(node, "exceptions", None) or ())
        cause = getattr(node, "__cause__", None)
        if cause is not None:
            pending.append(cause)
    return nodes


def _no_extra_details(node: Any) -> dict[str, Any]:
    return {}


@dataclass(frozen=True)
class TerminalProviderSignal:
    """One typed, terminal provider failure CLIO recognizes past LiteLLM's re-wrap.

    Args:
        reason: The typed ``details.reason`` a client can branch on.
        marker: CLIO's own stable text, the start of every message the owning
            provider module raises for this signal (never the provider's own
            prose directly -- see the module docstring).
        provider_id: The catalog id of the provider that raises it
            (``"claude_code"`` / ``"codex"``), used to label the ``ErrorInfo``.
        exception_type: The typed exception class this signal recognizes
            directly when it is still reachable (not yet re-wrapped).
        recovery_actions: The client's suggested recovery actions.
        extra_details: Structured fields beyond ``reason``/``provider_id`` to
            merge into ``ErrorInfo.details`` (e.g. the model, a rate-limit
            type, a provider request id). Called with the matched node (the
            live exception instance when ``exception_type`` matched directly,
            else the wrapped node whose text merely carried the marker) --
            implementations fall back to parsing the recovered clean message
            when the live object is not reachable. Defaults to no extra
            fields.
    """

    reason: str
    marker: str
    provider_id: str
    exception_type: type[BaseException]
    recovery_actions: tuple[str, ...] = ("retry",)
    extra_details: Callable[[Any], dict[str, Any]] = field(default=_no_extra_details)


def _matches(node: Any, signal: TerminalProviderSignal) -> bool:
    return isinstance(node, signal.exception_type) or signal.marker in str(node)


def find_terminal_signal(
    value: Any, signals: "tuple[TerminalProviderSignal, ...]"
) -> "tuple[TerminalProviderSignal, Any] | None":
    """The first registered signal ``value``'s tree matches, with its matching node.

    Args:
        value: The exception (or exception group) a failed turn raised.
        signals: The table to check against (typically
            :data:`clio_agent.providers.terminal_signal_catalog.TERMINAL_PROVIDER_SIGNALS`).

    Returns:
        ``(signal, node)`` for the first match -- ``node`` is the typed
        exception object when it is still reachable, else the wrapped node
        whose text carried the marker; or ``None`` when nothing matches.
    """
    for node in exception_tree(value):
        for signal in signals:
            if _matches(node, signal):
                return signal, node
    return None


def is_terminal_provider_error(value: Any, signals: "tuple[TerminalProviderSignal, ...]") -> bool:
    """Whether ``value`` carries any of ``signals`` (see :func:`find_terminal_signal`)."""
    return find_terminal_signal(value, signals) is not None


def recover_message(node: Any, marker: str) -> str:
    """CLIO's own clean sentence from ``node``, from ``marker`` to the traceback (or end).

    ``node`` is either the typed exception itself (``str(node)`` is already
    clean) or a wrapped node whose text contains the marker followed by
    LiteLLM's appended traceback -- slicing from the marker to
    :data:`TRACEBACK_MARKER` recovers exactly the sentence the provider module
    raised, never a re-interpretation of LiteLLM's or the provider's own
    words. A leading CLIO-owned recovery token before the marker (e.g. a
    ``[request_id=...]`` prefix some signals embed for their own
    ``extra_details`` to parse) is skipped, not included, in the returned
    sentence.
    """
    text = str(node)
    start = text.find(marker)
    if start == -1:
        return text.strip()
    end = text.find(TRACEBACK_MARKER, start)
    return text[start : end if end != -1 else len(text)].strip()
