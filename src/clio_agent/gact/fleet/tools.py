"""The agent's fleet tools: ``list_models`` and ``usage``.

Both read :func:`~clio_agent.gact.fleet.read.read_fleet`, the same reading
``GET /v1/fleet`` serves, so the agent and the person see one model list. The
agent chooses a model by reading these and passing ``model`` to a spawn, a
flow call or a workflow step; nothing chooses one behind its back.
"""

from __future__ import annotations

from typing import Any

from clio_agent.gact import context
from clio_agent.gact.agents.tool_instrumentation import native_tool
from clio_agent.gact.fleet.entries import FleetEntry
from clio_agent.gact.fleet.read import Fleet, read_fleet
from clio_agent.gact.fleet.selection import unmet_requirements
from clio_agent.gact.fleet.spend import tree_spend
from clio_agent.providers.usage.snapshot import UsageSnapshot
from clio_agent.sdk.model import Requires


def _app() -> Any:
    app = context.active_app()
    if app is None:
        raise ValueError("The fleet is read inside a product conversation")
    return app


def _price(entry: FleetEntry) -> str:
    inp, out = entry.price.get("input"), entry.price.get("output")
    if not (isinstance(inp, dict) and isinstance(out, dict)):
        return "price unknown"
    if {inp.get("kind"), out.get("kind")} == {"subscription"}:
        return "subscription"
    if inp.get("per_1m") is not None and out.get("per_1m") is not None:
        return f"${inp['per_1m']:g}/${out['per_1m']:g} per M in/out"
    return "price varies"


def _usage_line(snapshot: UsageSnapshot | None, billing: str) -> str:
    if snapshot is None:
        return "usage: no reading yet"
    if snapshot.state == "unmetered":
        return "unmetered"
    parts = [
        f"{w.name} {w.used_percent:g}% used" + (f", resets {w.resets_at}" if w.resets_at else "")
        for w in snapshot.windows
    ]
    if snapshot.balance is not None and snapshot.balance.remaining is not None:
        parts.append(f"{snapshot.balance.remaining:g} {snapshot.balance.currency} left")
    line = "; ".join(parts) or f"{billing}, no windows reported"
    if snapshot.read_failure:
        line += f" (last read failed: {snapshot.read_failure}; values from {snapshot.observed_at})"
    return line


def _row(entry: FleetEntry, fleet: Fleet) -> dict[str, Any]:
    account = fleet.account(entry.account)
    facts = [name for name, on in (("tools", entry.tool_calling), ("vision", entry.vision)) if on]
    row: dict[str, Any] = {
        "model": entry.ref,
        "facts": ", ".join(facts) or "text only",
        "context_tokens": entry.context_tokens,
        "price": _price(entry),
        "usage": _usage_line(fleet.usage.get(entry.account), account.billing if account else ""),
    }
    if entry.aliases:
        row["aliases"] = list(entry.aliases)
    if entry.effort_levels:
        row["effort"] = list(entry.effort_levels)
    if not entry.available:
        row["unavailable"] = entry.unavailable_reason
    return row


def _caller_root(app: Any, sid: str) -> str:
    sessions = app.state.sessions
    seen = {sid}
    current = sessions.get(sid)
    while (
        current is not None and current.parent_session_id and current.parent_session_id not in seen
    ):
        seen.add(current.parent_session_id)
        parent = sessions.get(current.parent_session_id)
        if parent is None:
            break
        current = parent
    return current.id if current is not None else sid


async def list_models(
    vision: bool = False,
    tool_calling: bool = False,
    min_context_tokens: int = 0,
    account: str = "",
    billing: str = "",
    include_unavailable: bool = False,
) -> dict[str, Any]:
    """List the models you can run work on: each as ``account/model`` (pass it as
    ``model`` to spawn_agent_task, spawn_agents_parallel, run_flow, run_workflow or a
    workflow step), with tool calling and vision, context size, price and the
    account's usage. Filter by what the work needs, an account or a billing kind
    (subscription, credit, metered, free). Prefer a smaller or free model for simple
    subtasks; check usage before a long fan-out."""

    fleet = await read_fleet(_app())
    need = Requires(
        tool_calling=tool_calling, vision=vision, context_tokens=max(0, min_context_tokens)
    )
    billing_of = {a.id: a.billing for a in fleet.accounts}
    rows = [
        _row(entry, fleet)
        for entry in fleet.entries
        if (include_unavailable or entry.available)
        and (not account or entry.account == account)
        and (not billing or billing_of.get(entry.account) == billing)
        and not unmet_requirements(entry, need)[0]
    ]
    return {"models": rows, "count": len(rows)}


async def usage(account: str = "") -> dict[str, Any]:
    """What each connected account has used and has left: its usage windows with
    reset times, its balance, and what this conversation and the work it started
    have spent per model. ``account`` narrows it to one account."""

    app = _app()
    fleet = await read_fleet(app)
    accounts = [a for a in fleet.accounts if not account or a.id == account]
    if account and not accounts:
        known = ", ".join(a.id for a in fleet.accounts) or "none"
        raise ValueError(f"no connected account {account!r}; connected: {known}")
    sid = context.active_session_id()
    spent = tree_spend(app, _caller_root(app, sid)) if sid else None
    return {
        "accounts": [
            {
                "account": a.id,
                "billing": a.billing,
                "health": a.health,
                "usage": _usage_line(fleet.usage.get(a.id), a.billing),
            }
            for a in accounts
        ],
        "this_conversation": {
            ref: spend.to_dict()
            for ref, spend in (spent.by_model.items() if spent else ())
            if not account or ref.startswith(f"{account}/")
        },
    }


def build_list_models_tool() -> Any:
    """The ``list_models`` tool."""

    return native_tool(
        list_models,
        name="list_models",
        title="List models",
        desc=list_models.__doc__,
        args={
            "vision": {"type": "boolean", "default": False},
            "tool_calling": {"type": "boolean", "default": False},
            "min_context_tokens": {"type": "integer", "minimum": 0, "default": 0},
            "account": {"type": "string", "default": ""},
            "billing": {
                "type": "string",
                "enum": ["", "subscription", "credit", "metered", "free"],
                "default": "",
            },
            "include_unavailable": {"type": "boolean", "default": False},
        },
        presentation="model_catalog",
        domain="providers",
    )


def build_usage_tool() -> Any:
    """The ``usage`` tool."""

    return native_tool(
        usage,
        name="usage",
        title="Account usage",
        desc=usage.__doc__,
        args={"account": {"type": "string", "default": ""}},
        presentation="model_catalog",
        domain="providers",
    )


__all__ = ["build_list_models_tool", "build_usage_tool", "list_models", "usage"]
