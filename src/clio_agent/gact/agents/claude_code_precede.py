"""B2 (S2 Claude SDK tuning): session-open Claude Code precede-connect hook.

Owner module (#775 no-accretion -- ``gact/agents/builders.py`` sits at its
1522-line ratchet ceiling, so the mechanism lives here, not there) for the one
step every claude_code-backed dynamic-agent module constructor performs
identically: the moment its resolved :class:`~clio_agent.config.LMProviderConfig`
and DSPy signature are BOTH in hand, hand the streaming pool the REAL
``model``/``thinking``/``system_prompt`` so the session's first turn finds an
already-connecting (or connected) client instead of paying full connect
latency inline (see :mod:`clio_agent.providers.claude_code_stream_bounds`'s
``precede_connect`` for the pool-side half of this contract).

Call site: :mod:`clio_agent.gact.agents.runners`, right after each of the
three module builders returns -- "the first time a session is resolved to a
claude_code model" in practice, since none of the three builders cache their
module across turns. :func:`precede_connect_claude_code_session` is safe to
call on EVERY turn: :meth:`~clio_agent.providers.claude_code_sessions
.ClaudeStreamClientPool.precede_connect` no-ops once the session already has
an entry (claimed or still pre-connecting), so only the session's actual
first call ever does real work here.

``system_prompt`` is a best-effort render of the adapter's own system message
(:meth:`dspy.Adapter.format_system_message`, defined once on the shared base
class and inherited unchanged by every CLIO adapter variant -- ChatAdapter,
its lenient subclass, and the guided-JSON subclass all resolve identically).
A miss (the module rebuilt its signature, or a call ends up on a different
adapter path) is safe by design: the pool's existing typed reconnect (B4/B13)
reconciles it the moment the real first turn's config is known -- this
function's job is only to give that reconnect a warm client to start from,
never to guarantee an exact match.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["precede_connect_claude_code_session"]


def precede_connect_claude_code_session(config: Any, signature: Any, *, session_id: str) -> None:
    """Fire a session-open precede-connect when ``config`` resolves to claude_code.

    A no-op for every other provider, an empty ``session_id`` (no active GACT
    session -- off-turn/test/CLI), a missing ``signature``, or any failure
    computing the thinking plan / system message -- this must NEVER raise
    into module construction or a turn.

    Args:
        config: The module's materialized :class:`LMProviderConfig` (``self.config``
            on every one of the three dynamic-agent module classes).
        signature: The DSPy signature this module's LM call will actually use.
        session_id: The GACT session id (the streaming pool's key, B1).
    """
    if not session_id or str(getattr(config, "provider", "") or "") != "claude_code":
        return
    if signature is None:
        return
    # The WHOLE resolve-and-call is one try/except: this hook must never break
    # module construction or a turn, regardless of which step misbehaves.
    try:
        import dspy  # noqa: PLC0415

        from clio_agent.providers.claude_code_sessions import _STREAM_CLIENT_POOL  # noqa: PLC0415
        from clio_agent.providers.reasoning_levels import model_effort_levels  # noqa: PLC0415
        from clio_agent.providers.thinking import resolve_thinking  # noqa: PLC0415

        model = str(getattr(config, "model", "") or "") or None
        plan = resolve_thinking(
            config.provider,
            getattr(config, "thinking_level", None),
            int(getattr(config, "thinking_budget", 0) or 0),
            effort_levels=model_effort_levels(config.provider, config.model or ""),
        )
        thinking = plan.sdk_thinking if plan.supported else None
        system_prompt = str(dspy.ChatAdapter().format_system_message(signature) or "") or None
        _STREAM_CLIENT_POOL.precede_connect(
            session_id=session_id, model=model, thinking=thinking, system_prompt=system_prompt
        )
    except Exception:  # noqa: BLE001 - this hook must never break the turn
        logger.debug("claude_code precede-connect hook failed", exc_info=True)
