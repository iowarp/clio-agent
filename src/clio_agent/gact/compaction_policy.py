"""What the model sees after a compaction: ONE editable research function.

A compaction never deletes anything (clio-core keeps every step, searchable and
recallable); it only changes the agent's context view. After it the model sees

    system prompt + summary + what is kept verbatim + the steps that come after.

:func:`post_compaction_context` decides, for one agent scope's live segments, which ids
the summary replaces and which stay verbatim. The summary always renders first, ahead
of everything kept (``compaction.compact_session_context`` places it at position 0).
The default keeps only the current user question (``keep_head``): system + summary +
the question verbatim + new steps. Change the rule here, or tune it without code via
the ``compaction.keep.*`` config keys (:func:`keep_policy`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from clio_agent import conf

__all__ = [
    "KeepPolicy",
    "PostCompactionContext",
    "keep_policy",
    "post_compaction_context",
]

_STEP_KINDS = frozenset({"thought", "tool_call", "observation"})


@dataclass(frozen=True)
class KeepPolicy:
    """What a compaction keeps verbatim.

    Attributes:
        keep_head: Keep the open turn's user question (mid-turn only: between turns
            there is no current question).
        keep_last_turns: Keep the last N whole turns before the open turn (between
            turns: the last N turns).
        keep_last_steps: Keep the last N ReAct steps of the latest turn (the open one
            mid-turn).
    """

    keep_head: bool = True
    keep_last_turns: int = 0
    keep_last_steps: int = 0


@dataclass(frozen=True)
class PostCompactionContext:
    """The policy's decision for one scope.

    Attributes:
        summarize: The live ids the summary replaces, in live order.
        keep: The live ids kept verbatim after the summary, in live order.
    """

    summarize: tuple[str, ...]
    keep: tuple[str, ...]


def keep_policy() -> KeepPolicy:
    """The configured :class:`KeepPolicy` (file -> env -> default)."""
    return KeepPolicy(
        keep_head=conf.resolve(
            "compaction.keep.head",
            env="CLIO_COMPACTION_KEEP_HEAD",
            default=True,
            cast=conf.as_bool,
        ),
        keep_last_turns=conf.resolve(
            "compaction.keep.last_turns",
            env="CLIO_COMPACTION_KEEP_LAST_TURNS",
            default=0,
            cast=conf.as_int,
        ),
        keep_last_steps=conf.resolve(
            "compaction.keep.last_steps",
            env="CLIO_COMPACTION_KEEP_LAST_STEPS",
            default=0,
            cast=conf.as_int,
        ),
    )


@dataclass
class _Unit:
    """Segments that are kept or summarized together (a whole step, or one message)."""

    kind: str  # "step" | "user" | "summary"
    turn_id: str
    ids: list[str]
    head: bool = False  # the forward's own user question (no ``actor``)


def post_compaction_context(
    live: Sequence[Any], *, open_turn_id: str, policy: KeepPolicy
) -> PostCompactionContext:
    """Split one scope's live segments into what is summarized and what is kept.

    Args:
        live: The scope's live segments in render order.
        open_turn_id: The running turn's id (``""`` between turns).
        policy: What to keep verbatim.

    Returns:
        The ids to summarize and the ids to keep. A step is kept or summarized whole
        (its thought, calls and results), so the kept context always folds; a summary
        from an earlier compaction is always summarized again.
    """
    units = _units(live)
    turns = _turn_order(units, open_turn_id)
    kept: set[int] = set()
    if policy.keep_head and open_turn_id:
        heads = [i for i, u in enumerate(units) if u.head and u.turn_id == open_turn_id]
        kept.update(heads[-1:])
    if policy.keep_last_turns > 0:
        keep_turns = set(turns[-policy.keep_last_turns :])
        kept.update(
            i for i, u in enumerate(units) if u.kind != "summary" and u.turn_id in keep_turns
        )
    if policy.keep_last_steps > 0:
        latest = open_turn_id or (turns[-1] if turns else "")
        steps = [i for i, u in enumerate(units) if u.kind == "step" and u.turn_id == latest]
        kept.update(steps[-policy.keep_last_steps :])
    summarize = [sid for i, u in enumerate(units) if i not in kept for sid in u.ids]
    keep = [sid for i, u in enumerate(units) if i in kept for sid in u.ids]
    return PostCompactionContext(summarize=tuple(summarize), keep=tuple(keep))


def _units(live: Sequence[Any]) -> list[_Unit]:
    units: list[_Unit] = []
    for seg in live:
        kind = str(getattr(seg, "kind", ""))
        turn = str(getattr(seg, "turn_id", "") or "")
        if kind in _STEP_KINDS and kind != "thought" and units and units[-1].kind == "step":
            units[-1].ids.append(seg.id)
        elif kind in _STEP_KINDS:
            units.append(_Unit("step", turn, [seg.id]))
        elif kind == "user":
            content = getattr(seg, "content", None)
            head = isinstance(content, Mapping) and "actor" not in content
            units.append(_Unit("user", turn, [seg.id], head=head))
        else:
            units.append(_Unit("summary", turn, [seg.id]))
    return units


def _turn_order(units: Sequence[_Unit], open_turn_id: str) -> list[str]:
    """The finished turns in live order (the open turn excluded)."""
    order: list[str] = []
    for unit in units:
        if unit.kind != "summary" and unit.turn_id != open_turn_id and unit.turn_id not in order:
            order.append(unit.turn_id)
    return order
