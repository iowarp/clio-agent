"""Session creation's run setup: the reserved metadata, the flow, unattended trust.

``POST /v1/sessions`` calls :func:`prepare_run` off the loop in place of a bare
``prepare_session_agent``. It resolves the agent (trusting its marketplace first when
an unattended run names an untrusted one as ``marketplace/id``), admits ``flow``
through the same check ``run_flow`` uses (adding the plugin that provides it when the
agent lacks it, and trusting an untrusted one only in an unattended run), and writes
the server-owned metadata: ``agent_flow`` without ``input`` or ``depth`` (the
agent's-own-flow branch of :func:`clio_agent.flows.runner.run_agent_flow`),
``unattended`` and ``run_limits``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException

from clio_agent.gact.types import ErrorEnvelope, ErrorInfo
from clio_agent.gact.unattended import (
    RUN_LIMITS_KEY,
    UNATTENDED_KEY,
    record_trusted,
    reserved_keys,
)


class FlowRefused(RuntimeError):
    """A session's ``flow`` cannot start; ``refusal`` is ``run_flow``'s typed refusal."""

    def __init__(self, refusal: dict[str, Any]) -> None:
        super().__init__(str(refusal.get("error") or refusal.get("reason")))
        self.refusal = refusal


@dataclass
class PreparedRun:
    """The resolved agent and the marketplaces trusted for this run."""

    resolution: Any
    trusted: list[str] = field(default_factory=list)


def refuse_reserved_metadata(metadata: dict[str, Any] | None) -> None:
    """422 when a client's ``metadata`` carries keys only the server sets."""

    keys = reserved_keys(metadata)
    if not keys:
        return
    raise HTTPException(
        status_code=422,
        detail=ErrorEnvelope(
            error=ErrorInfo(
                error="metadata_reserved",
                message=(
                    f"metadata may not set {', '.join(keys)}: the server sets them "
                    "(a session runs a flow through the 'flow' field)"
                ),
                details={"keys": keys},
                recoverable=True,
            )
        ).model_dump(exclude_none=True),
    )


def flow_refused_envelope(exc: FlowRefused) -> dict[str, Any]:
    """The 422 body for a refused ``flow``: the same typed reason ``run_flow`` returns."""

    reason = str(exc.refusal.get("reason") or "flow_refused")
    return ErrorEnvelope(
        error=ErrorInfo(
            error=reason,
            message=str(exc),
            details={k: v for k, v in exc.refusal.items() if k not in {"accepted", "error"}},
            recoverable=True,
        )
    ).model_dump(exclude_none=True)


def _trust(app: Any, market: str, *, snapshot: str = "") -> None:
    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415
    from clio_agent.gact.plugin_authoring import installed_changed  # noqa: PLC0415

    plugin_service(app).store.trust(market, reason="unattended_run", snapshot=snapshot)
    installed_changed(app)


def _untrusted_market(app: Any, requested: str) -> str:
    """The installed, untrusted marketplace a ``marketplace/id`` agent ref names, or ''."""

    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415

    market = requested.split("/", 1)[0] if "/" in requested else ""
    entry = plugin_service(app).store.marketplaces().get(market) if market else None
    return market if entry is not None and not entry.get("trusted") else ""


def _flow_plugin_refs(app: Any, resolution: Any, flow_id: str, *, unattended: bool) -> tuple:
    """``(refs to add, marketplace trusted)`` so the agent can run ``flow_id``."""

    from clio_agent.gact.agents.resolution import plugin_service  # noqa: PLC0415
    from clio_agent.gact.plugin_authoring import outside_flows, untrusted_flow  # noqa: PLC0415

    store = plugin_service(app).store
    resolved = {row["id"] for row in resolution.flows}
    if flow_id in resolved:
        return [], ""
    outside = next((r for r in outside_flows(store, resolved) if r["flow"] == flow_id), None)
    if outside is not None:
        return [outside["ref"]], ""
    found = untrusted_flow(store, flow_id)
    if found is None:
        return [], ""  # flow_refusal names it as not resolved
    if not unattended:
        raise FlowRefused(
            {
                "reason": "flow_not_trusted",
                "error": f"flow {flow_id!r} is from the {found['plugin']} plugin, not trusted "
                "yet; trust it in Settings or with `clio-agent plugin trust`",
                "plugin": found["plugin"],
            }
        )
    _trust(app, found["plugin"], snapshot=found["snapshot"])
    refs = sorted(
        f"{row.marketplace}/{row.id}"
        for row in store.records()
        if row.marketplace == found["plugin"]
    )
    return refs, found["plugin"]


def prepare_run(app: Any, req: Any, metadata: dict[str, Any], requested: str) -> PreparedRun:
    """Resolve the session's agent and flow and write the run metadata (in place).

    Raises:
        SessionAgentUnavailable: The agent (or the agent with the flow's plugin) does
            not resolve.
        FlowRefused: ``flow`` cannot start; carries ``run_flow``'s typed refusal.
    """

    from clio_agent.flows.values import _plain  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        AGENT_DELTA_KEY,
        prepare_session_agent,
    )
    from clio_agent.gact.flow_tools import flow_refusal  # noqa: PLC0415

    trusted: list[str] = []
    if req.unattended and (market := _untrusted_market(app, requested)):
        _trust(app, market)
        trusted.append(market)
    resolution = prepare_session_agent(app, metadata, requested)
    if req.flow is not None:
        refs, market = _flow_plugin_refs(app, resolution, req.flow.id, unattended=req.unattended)
        if market:
            trusted.append(market)
        if refs:
            delta = dict(metadata.get(AGENT_DELTA_KEY) or {})
            added = [*delta.get("add", []), *(r for r in refs if r not in delta.get("add", []))]
            metadata[AGENT_DELTA_KEY] = {"add": added, "remove": list(delta.get("remove", []))}
            resolution = prepare_session_agent(app, metadata, requested)
        refusal = flow_refusal(resolution, req.flow.id, req.flow.options)
        if refusal is not None:
            raise FlowRefused(refusal)
        metadata["agent_flow"] = {"id": req.flow.id, "options": _plain(req.flow.options)}
    if req.unattended:
        metadata[UNATTENDED_KEY] = True
    if limits := req.run_limits():
        metadata[RUN_LIMITS_KEY] = limits
    return PreparedRun(resolution, trusted)


def record_run_trust(app: Any, session_id: str, prepared: PreparedRun) -> None:
    """Record on the new session the marketplaces trusted while preparing it."""

    for market in prepared.trusted:
        record_trusted(app, session_id, market, marketplace=market)


__all__ = [
    "FlowRefused",
    "PreparedRun",
    "flow_refused_envelope",
    "prepare_run",
    "record_run_trust",
    "refuse_reserved_metadata",
]
