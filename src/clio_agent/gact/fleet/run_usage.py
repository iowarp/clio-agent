"""A flow run's usage, for the summary under its railroad.

Which models the run used, each with its share of the run's tokens and its
cost, the busiest window of each account it ran on, and per call the model
its child runs on and why (its ``model_choice``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.gact.fleet.selection import MODEL_CHOICE_KEY
from clio_agent.gact.fleet.spend import tree_spend
from clio_agent.providers.catalog import get_provider
from clio_agent.providers.usage.snapshot import USAGE_BOARD

if TYPE_CHECKING:
    from fastapi import FastAPI


def _call_model(app: "FastAPI", handle: str) -> dict[str, Any] | None:
    snapshot = app.state.task_history.task_snapshot(handle) if handle else None
    session = app.state.sessions.get(snapshot.owner.session_id) if snapshot is not None else None
    if session is None:
        return None
    choice = (session.metadata or {}).get(MODEL_CHOICE_KEY)
    model = dict(session.model or {})
    return {
        "model": f"{model.get('provider_id')}/{model.get('model_id')}"
        if model.get("provider_id") and model.get("model_id")
        else "",
        "choice": choice if isinstance(choice, dict) else None,
    }


def call_models(app: "FastAPI", calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Per call handle, the model its child runs on and its ``model_choice``."""

    return {
        str(call.get("handle")): found
        for call in calls
        if call.get("handle") and (found := _call_model(app, str(call["handle"]))) is not None
    }


def run_spend(app: "FastAPI", run_session: str) -> dict[str, Any]:
    """Models with their token share and cost, and each account's busiest window,
    over ``run_session``'s session tree."""

    report = tree_spend(app, run_session)
    # A model's share is of the tokens it read and wrote; cache reads repeat the
    # same prompt on every call and would drown the work itself.
    total = sum(spend.tokens_input + spend.tokens_output for spend in report.by_model.values())
    models = sorted(
        (
            {
                "model": ref,
                "tokens": spend.tokens,
                "share": round((spend.tokens_input + spend.tokens_output) / total, 3)
                if total
                else 0.0,
                "cost_usd": round(spend.cost_usd, 6),
            }
            for ref, spend in report.by_model.items()
        ),
        key=lambda row: -row["share"],
    )
    windows = []
    for account in sorted({row["model"].split("/", 1)[0] for row in models}):
        snapshot = USAGE_BOARD.latest(account)
        if snapshot is None or not snapshot.windows:
            continue
        busiest = max(snapshot.windows, key=lambda window: window.used_percent)
        preset = get_provider(account)
        windows.append(
            {
                "account": account,
                "label": preset.label if preset is not None else account,
                "window": busiest.name,
                "used_percent": busiest.used_percent,
            }
        )
    return {
        "models": models,
        "windows": windows,
        "estimated_sessions": len(report.estimated_sessions),
    }


__all__ = ["call_models", "run_spend"]
