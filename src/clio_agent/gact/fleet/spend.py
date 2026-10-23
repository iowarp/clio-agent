"""Spend: tokens and cost by model, summed from the transcript itself.

Every assistant message already records the tokens and cost of its turn and
the model that ran it (``metadata.agent_runtime.model``). Summing those gives
spend by account, by model, by session and by session tree with no store of
its own, and it covers accounts with no balance endpoint and local models
(their "tokens served").
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
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

    def add(self, tokens: Mapping[str, Any], cost: Any) -> None:
        self.tokens_input += int(tokens.get("input") or 0)
        self.tokens_cached += int(tokens.get("cache_read") or 0) + int(
            tokens.get("cache_write") or 0
        )
        self.tokens_output += int(tokens.get("output") or 0)
        if isinstance(cost, int | float) and not isinstance(cost, bool):
            self.cost_usd += float(cost)
        else:
            self.turns_without_cost += 1
        self.turns += 1

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


def _field(message: Any, name: str) -> Any:
    return message.get(name) if isinstance(message, Mapping) else getattr(message, name, None)


def message_model_ref(message: Any) -> str:
    """``account/model`` of the model that produced an assistant message, or ``""``."""

    metadata = _field(message, "metadata")
    runtime = metadata.get("agent_runtime") if isinstance(metadata, Mapping) else None
    model = runtime.get("model") if isinstance(runtime, Mapping) else None
    if not isinstance(model, Mapping):
        return ""
    provider, model_id = str(model.get("provider_id") or ""), str(model.get("model_id") or "")
    return f"{provider}/{model_id}" if provider and model_id else ""


def spend_by_model(messages: Iterable[Any]) -> dict[str, Spend]:
    """Spend per ``account/model`` over ``messages`` (assistant turns only)."""

    totals: dict[str, Spend] = {}
    for message in messages:
        if _field(message, "role") != "assistant":
            continue
        tokens = _field(message, "tokens")
        if hasattr(tokens, "model_dump"):
            tokens = tokens.model_dump()
        if not isinstance(tokens, Mapping):
            continue
        ref = message_model_ref(message) or "unknown/unknown"
        totals.setdefault(ref, Spend()).add(tokens, _field(message, "cost_usd"))
    return totals


def session_tree(app: "FastAPI", root_sid: str) -> list[str]:
    """``root_sid`` and every session spawned beneath it."""

    sessions = app.state.sessions
    children: dict[str, list[str]] = {}
    for session in sessions.list():
        if session.parent_session_id:
            children.setdefault(session.parent_session_id, []).append(session.id)
    tree, frontier = [], [root_sid]
    while frontier:
        sid = frontier.pop()
        tree.append(sid)
        frontier.extend(children.get(sid, []))
    return tree


def _session_messages(app: "FastAPI", sid: str) -> list[Any]:
    """A session's messages, read without loading it into the resident set."""

    ledgers = app.state.messages
    resident = ledgers.get_if_resident(sid) if hasattr(ledgers, "get_if_resident") else None
    if resident is not None:
        return list(resident)
    if not hasattr(ledgers, "get_if_resident"):
        return list(ledgers.get(sid, []))
    from clio_agent.gact.transcript_projection import materialize_ledger  # noqa: PLC0415

    return list(materialize_ledger(app, sid) or [])


def _cached_session_spend(app: "FastAPI", session: Any) -> dict[str, Spend]:
    """Spend per model for one session, recomputed only when the session changed."""

    cache: dict[str, tuple[str, dict[str, Spend]]] | None = getattr(
        app.state, "fleet_spend_cache", None
    )
    if cache is None:
        cache = app.state.fleet_spend_cache = {}
    stamp = f"{session.updated_at}|{session.message_count}"
    hit = cache.get(session.id)
    if hit is not None and hit[0] == stamp:
        return hit[1]
    totals = spend_by_model(_session_messages(app, session.id))
    cache[session.id] = (stamp, totals)
    return totals


@dataclass
class SpendReport:
    """Spend per model over some sessions, and the sessions that could not be read."""

    by_model: dict[str, Spend]
    unreadable_sessions: list[str]


def _sum(app: "FastAPI", sessions: Iterable[Any]) -> SpendReport:
    from clio_agent.gact.messages import LedgerReadError  # noqa: PLC0415

    totals: dict[str, Spend] = {}
    unreadable: list[str] = []
    for session in sessions:
        try:
            per_session = _cached_session_spend(app, session)
        except LedgerReadError:
            unreadable.append(session.id)
            continue
        for ref, spend in per_session.items():
            totals.setdefault(ref, Spend()).merge(spend)
    return SpendReport(totals, unreadable)


def tree_spend(app: "FastAPI", root_sid: str) -> SpendReport:
    """Spend per model over a session and everything it spawned."""

    tree = set(session_tree(app, root_sid))
    return _sum(app, (s for s in app.state.sessions.list() if s.id in tree))


def workspace_spend(app: "FastAPI") -> SpendReport:
    """Spend per model over every session this server holds."""

    return _sum(app, app.state.sessions.list())


def spend_by_account(by_model: Mapping[str, Spend]) -> dict[str, Spend]:
    """Fold per-model spend into per-account spend."""

    totals: dict[str, Spend] = {}
    for ref, spend in by_model.items():
        totals.setdefault(ref.split("/", 1)[0], Spend()).merge(spend)
    return totals


__all__ = [
    "Spend",
    "SpendReport",
    "message_model_ref",
    "session_tree",
    "spend_by_account",
    "spend_by_model",
    "tree_spend",
    "workspace_spend",
]
