"""Spend: tokens and cost by model, from the sessions' own rollups.

Each session records its tokens and cost per ``account/model`` as its turns
finish (``Session.usage_by_model``, written with the session's totals). Summing
those gives spend by account, by model, by session and by session tree without
reading a transcript, and covers accounts with no balance endpoint and local
models (their "tokens served").

A session from before the per-model rollup has only its totals; those are
counted under the model the session was pinned to (``unknown`` when none), and
the report says how many sessions were counted that way.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


@dataclass
class Spend:
    """Tokens and cost of a set of turns."""

    tokens_input: int = 0
    tokens_cached: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    #: Turns whose cost was not reported (a subscription, a local model).
    turns_without_cost: int = 0
    turns: int = 0

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "Spend":
        return cls(
            tokens_input=int(row.get("tokens_input") or 0),
            tokens_cached=int(row.get("tokens_cached") or 0),
            tokens_output=int(row.get("tokens_output") or 0),
            cost_usd=float(row.get("cost_usd") or 0.0),
            turns_without_cost=int(row.get("turns_without_cost") or 0),
            turns=int(row.get("turns") or 0),
        )

    def merge(self, other: "Spend") -> None:
        self.tokens_input += other.tokens_input
        self.tokens_cached += other.tokens_cached
        self.tokens_output += other.tokens_output
        self.cost_usd += other.cost_usd
        self.turns_without_cost += other.turns_without_cost
        self.turns += other.turns

    @property
    def tokens(self) -> int:
        return self.tokens_input + self.tokens_cached + self.tokens_output

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "tokens": self.tokens, "cost_usd": round(self.cost_usd, 6)}


@dataclass
class SpendReport:
    """Spend per model over some sessions."""

    by_model: dict[str, Spend]
    #: Sessions from before the per-model rollup, counted under their pinned model.
    estimated_sessions: list[str] = field(default_factory=list)


def session_spend(session: Any) -> tuple[dict[str, Spend], bool]:
    """``(spend per model, estimated)`` for one session."""

    rows = getattr(session, "usage_by_model", None) or {}
    if rows:
        return {ref: Spend.from_row(row) for ref, row in rows.items()}, False
    tokens_in = int(getattr(session, "tokens_input", 0) or 0)
    tokens_out = int(getattr(session, "tokens_output", 0) or 0)
    if not (tokens_in or tokens_out):
        return {}, False
    model = dict(getattr(session, "model", None) or {})
    ref = (
        f"{model['provider_id']}/{model['model_id']}"
        if model.get("provider_id") and model.get("model_id")
        else "unknown/unknown"
    )
    known = bool(getattr(session, "cost_known", False))
    return {
        ref: Spend(
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            cost_usd=float(getattr(session, "cost_usd", 0.0) or 0.0) if known else 0.0,
            turns_without_cost=0 if known else 1,
            turns=1,
        )
    }, True


def sum_sessions(sessions: Iterable[Any]) -> SpendReport:
    """Spend per model over ``sessions``."""

    totals: dict[str, Spend] = {}
    estimated: list[str] = []
    for session in sessions:
        per_model, guessed = session_spend(session)
        if guessed:
            estimated.append(str(getattr(session, "id", "")))
        for ref, spend in per_model.items():
            totals.setdefault(ref, Spend()).merge(spend)
    return SpendReport(totals, estimated)


def session_tree(app: "FastAPI", root_sid: str) -> list[str]:
    """``root_sid`` and every session spawned beneath it."""

    children = app.state.sessions.child_ids_by_parent()  # the index: no row is read
    tree, frontier = [], [root_sid]
    while frontier:
        sid = frontier.pop()
        tree.append(sid)
        frontier.extend(children.get(sid, []))
    return tree


def tree_spend(app: "FastAPI", root_sid: str) -> SpendReport:
    """Spend per model over a session and everything it spawned."""

    from clio_agent.gact.session_records import PageQuery  # noqa: PLC0415

    tree = frozenset(session_tree(app, root_sid))
    return sum_sessions(app.state.sessions.select(PageQuery(archived=None, ids=tree)))


def workspace_spend(app: "FastAPI") -> SpendReport:
    """Spend per model over every session this server holds (reads every record)."""
    from clio_agent.gact.session_records import PageQuery  # noqa: PLC0415

    return sum_sessions(app.state.sessions.select(PageQuery(archived=None)))


def spend_by_account(by_model: Mapping[str, Spend]) -> dict[str, Spend]:
    """Fold per-model spend into per-account spend."""

    totals: dict[str, Spend] = {}
    for ref, spend in by_model.items():
        totals.setdefault(ref.split("/", 1)[0], Spend()).merge(spend)
    return totals


__all__ = [
    "Spend",
    "SpendReport",
    "session_spend",
    "session_tree",
    "spend_by_account",
    "sum_sessions",
    "tree_spend",
    "workspace_spend",
]
