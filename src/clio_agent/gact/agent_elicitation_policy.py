"""Agent-driven elicitation config knobs (#1309, C1-S7; #1331 size-ratchet split).

Split out of :mod:`clio_agent.gact.agent_elicitation` (the cleanup program's
no-accretion rule, #775: that module is a ratchet-baselined file — new logic
goes in an owner module of its own, not appended past its recorded line
count) as its own small, focused owner for exactly one concern: the
config-over-env policy knobs that gate agent-audience elicitation routing and
its bounded answer invocation. Moved verbatim, no behavior change.
"""

from __future__ import annotations

from clio_agent import conf

_DEFAULT_MAX_DEPTH = 1
_DEFAULT_TIMEOUT_S = 90.0
_OUTER_TIMEOUT_MARGIN_S = 15.0


# --------------------------------------------------------------------------- #
# Policy (config-over-env; the house pattern -- clio_agent.conf)              #
# --------------------------------------------------------------------------- #


def _enabled() -> bool:
    """Global on/off for agent-audience routing (default ON, owner-ruled posture)."""

    return conf.resolve(
        "tools.mcp.elicitation.agent_audience.enabled",
        env="CLIO_MCP_ELICITATION_AGENT_AUDIENCE_ENABLED",
        default=True,
        cast=conf.as_bool,
    )


def _denied_servers() -> frozenset[str]:
    """Per-server opt-OUT list (namespace names) -- the policy knob the owner required."""

    default: list[str] = []
    return frozenset(
        conf.resolve(
            "tools.mcp.elicitation.agent_audience.denied_servers",
            env="CLIO_MCP_ELICITATION_AGENT_AUDIENCE_DENIED_SERVERS",
            default=default,
            cast=conf.as_csv,
        )
    )


def _max_depth() -> int:
    """The bounded agent-answer recursion depth (default 1)."""

    return conf.resolve(
        "tools.mcp.elicitation.agent_audience.max_depth",
        env="CLIO_MCP_ELICITATION_AGENT_AUDIENCE_MAX_DEPTH",
        default=_DEFAULT_MAX_DEPTH,
        cast=conf.as_int,
    )


def _timeout_s() -> float:
    """Bounded wall-clock budget for one agent-answer child turn."""

    return conf.resolve(
        "tools.mcp.elicitation.agent_audience.timeout_s",
        env="CLIO_MCP_ELICITATION_AGENT_AUDIENCE_TIMEOUT_S",
        default=_DEFAULT_TIMEOUT_S,
        cast=conf.as_float,
    )


def _answer_mode() -> str:
    """Which answerer fulfills a routed agent-audience elicitation.

    ``"inline"`` (default): a bounded tool-less completion off the event loop.
    ``"turn"``: the child-turn spawn, which deadlocks while the parent tool
    call holds the session's turn slot (see the module docstring).
    """

    return (
        conf.resolve(
            "tools.mcp.elicitation.agent_audience.answer_mode",
            env="CLIO_MCP_ELICITATION_AGENT_AUDIENCE_ANSWER_MODE",
            default="inline",
            cast=conf.as_str,
        )
        .strip()
        .lower()
    )


def _default_unhinted() -> bool:
    """Whether an UNHINTED form elicitation defaults to the agent (default ON).

    fastmcp's ``InputRequiredResult`` round-trip drops the nested ``_meta``, so
    an explicit ``x-clio-agent/audience`` tag never survives MRTR -- ``audience``
    arrives empty. Default ON so agent-driven elicitation functions at all over
    MRTR; set off to force strict explicit-tag routing.
    """

    return conf.resolve(
        "tools.mcp.elicitation.agent_audience.default_unhinted",
        env="CLIO_MCP_ELICITATION_AGENT_AUDIENCE_DEFAULT_UNHINTED",
        default=True,
        cast=conf.as_bool,
    )
