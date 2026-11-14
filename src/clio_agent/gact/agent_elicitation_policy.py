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


#: CLIO's own servers whose questions only the person may answer: approving where
#: search queries go and choosing what to do when a free allowance runs out.
PERSON_ONLY_SERVERS = frozenset({"web", "literature"})


def _denied_servers() -> frozenset[str]:
    """Per-server opt-OUT list (namespace names) -- the policy knob the owner required.

    :data:`PERSON_ONLY_SERVERS` are always included; configuration cannot route
    their questions to an agent.
    """

    default: list[str] = []
    configured = conf.resolve(
        "tools.mcp.elicitation.agent_audience.denied_servers",
        env="CLIO_MCP_ELICITATION_AGENT_AUDIENCE_DENIED_SERVERS",
        default=default,
        cast=conf.as_csv,
    )
    return frozenset(configured) | PERSON_ONLY_SERVERS


def _max_depth() -> int:
    """The bounded agent-answer recursion depth (default 1)."""

    return conf.resolve(
        "tools.mcp.elicitation.agent_audience.max_depth",
        env="CLIO_MCP_ELICITATION_AGENT_AUDIENCE_MAX_DEPTH",
        default=_DEFAULT_MAX_DEPTH,
        cast=conf.as_int,
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
