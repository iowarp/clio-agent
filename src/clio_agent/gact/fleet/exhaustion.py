"""When a usage window runs out: say whose choice the model was and what else can run.

A plan-limit or credit rejection already arrives as a typed provider error
(``claude_code_plan_limit``, ``codex_plan_limit``). This adds
``details.model_exhausted``: the model, when its window resets, who chose it,
and the connected models that can run now. There is never an automatic switch:

* a model Clio chose (a child's explicit, preferred or inherited model) fails
  the child's assignment with that record, so the parent's model reads
  ``model_exhausted`` with the reset time and decides where the work goes next;
* a model the person chose fails the turn with the alternatives as the error's
  choices, and nothing changes until the person picks one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from clio_agent.gact.fleet.selection import MODEL_CHOICE_KEY
from clio_agent.providers.usage.snapshot import epoch_iso

if TYPE_CHECKING:
    from fastapi import FastAPI

PLAN_LIMIT_REASONS = frozenset({"claude_code_plan_limit", "codex_plan_limit"})


def model_exhausted(app: "FastAPI", sid: str, details: dict[str, Any]) -> dict[str, Any] | None:
    """The ``model_exhausted`` record for a plan-limit error in session ``sid``, or ``None``."""

    if details.get("reason") not in PLAN_LIMIT_REASONS:
        return None
    from clio_agent.gact.fleet.child_model import fleet_entries  # noqa: PLC0415

    session = app.state.sessions.get(sid)
    metadata = dict(getattr(session, "metadata", None) or {})
    choice = metadata.get(MODEL_CHOICE_KEY)
    chooser = "clio" if isinstance(choice, dict) and choice.get("source") != "person" else "person"
    model = dict(getattr(session, "model", None) or {})
    account = str(model.get("provider_id") or details.get("provider_id") or "")
    model_id = str(model.get("model_id") or details.get("model") or "")
    alternatives = sorted(
        entry.ref
        for entry in fleet_entries(app, needed=False)
        if entry.available and entry.account != account
    )
    resets_at = details.get("resets_at")
    return {
        "model": f"{account}/{model_id}" if account and model_id else model_id,
        "resets_at": epoch_iso(resets_at) if isinstance(resets_at, int | float) else "",
        "chooser": chooser,
        "alternatives": alternatives,
    }


def exhausted_message(record: dict[str, Any], message: str) -> str:
    """``message`` with what to do next, for the model that reads a child's failure."""

    when = f" until {record['resets_at']}" if record.get("resets_at") else ""
    others = ", ".join(record.get("alternatives") or []) or "no other connected model"
    return (
        f"model_exhausted: {record['model']} cannot run{when}. {message} "
        f"Models that can run now: {others}."
    )


__all__ = ["PLAN_LIMIT_REASONS", "exhausted_message", "model_exhausted"]
