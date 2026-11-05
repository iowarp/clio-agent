"""What the model sees after a compaction: ONE editable research function.

A compaction never deletes anything (clio-core keeps every step, searchable and
recallable); it only changes the agent's context view. After it the model sees

    system prompt + summary + what is kept verbatim + the steps that come after.

:func:`post_compaction_context` decides, for one agent scope's live segments, which ids
the summary replaces and which stay verbatim. The summary always renders first, ahead
of everything kept (``compaction.compact_session_context`` places it at position 0).
The default keeps every message the person typed (newest first, each that still fits
``compaction.keep.user_chars`` measured on the text the model reads, so a confirmed
input counts its content, not its reference), the newest step of the running turn and
the latest working state, so the request, its limits and the output the agent was using
stay verbatim: system + summary + those + new steps. Every other harness reviewed keeps the user's messages or a recent tail
(``docs/design/compaction-review-2026-11.md``). Change the rule here, or tune it without
code via the ``compaction.keep.*`` config keys (:func:`keep_policy`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from clio_agent import conf

__all__ = [
    "KeepPolicy",
    "PostCompactionContext",
    "ReadContent",
    "keep_policy",
    "post_compaction_context",
    "transcript_rows",
]

_STEP_KINDS = frozenset({"thought", "tool_call", "observation"})
#: Reads the content a confirmed input's reference names (``TaskHistory.read_content``).
ReadContent = Callable[[Any], Any]


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
        keep_user_chars: Keep the messages the person typed, newest first, each one whose
            text still fits in this many characters (one too long for what is left is
            summarized and older ones that fit are still kept; 0: only ``keep_head``).
        keep_working_state: Keep the latest plan, todos and plan-mode note verbatim, and
            offer them again after a mid-turn compaction (off only to measure what that
            re-offer is worth: the summary alone then carries them).
    """

    keep_head: bool = True
    keep_last_turns: int = 0
    keep_last_steps: int = 1
    keep_user_chars: int = 80_000
    keep_working_state: bool = True


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
            default=1,
            cast=conf.as_int,
        ),
        keep_user_chars=conf.resolve(
            "compaction.keep.user_chars",
            env="CLIO_COMPACTION_KEEP_USER_CHARS",
            default=80_000,
            cast=conf.as_int,
        ),
        keep_working_state=conf.resolve(
            "compaction.keep.working_state",
            env="CLIO_COMPACTION_KEEP_WORKING_STATE",
            default=True,
            cast=conf.as_bool,
        ),
    )


@dataclass
class _Unit:
    """Segments that are kept or summarized together (a whole step, or one message)."""

    kind: str  # "step" | "user" | "summary"
    turn_id: str
    ids: list[str]
    head: bool = False  # the forward's own user question (no ``actor``)
    source: str = ""  # an injection's source (``todos``, ``plan``, ...)
    typed: bool = False  # a message the requester wrote (a question or a steer)
    seg: Any = None  # a message's segment (the typed-message budget measures its text)


def post_compaction_context(
    live: Sequence[Any],
    *,
    open_turn_id: str,
    policy: KeepPolicy,
    read_content: ReadContent | None = None,
) -> PostCompactionContext:
    """Split one scope's live segments into what is summarized and what is kept.

    Args:
        live: The scope's live segments in render order.
        open_turn_id: The running turn's id (``""`` between turns).
        policy: What to keep verbatim.
        read_content: Resolves a confirmed input's content reference, so the typed-message
            budget measures the message, not its reference (needed when a typed message
            is held by reference and the budget is above 0).

    Raises:
        ContextReadError: a typed message is held by reference and no ``read_content``.

    Returns:
        The ids to summarize and the ids to keep. A step is kept or summarized whole
        (its thought, calls and results), so the kept context always folds; a summary
        from an earlier compaction is always summarized again.
    """
    units = _units(live)
    turns = _turn_order(units, open_turn_id)
    kept: set[int] = set()
    # Working state (plan mode, the plan, the todos) is never summarized: its latest
    # copy per source stays verbatim, and the loop re-offers it after the compaction.
    from clio_agent.gact.working_state import WORKING_STATE_SOURCES  # noqa: PLC0415

    if policy.keep_working_state:
        newest = {u.source: i for i, u in enumerate(units) if u.source in WORKING_STATE_SOURCES}
        kept.update(newest.values())
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
    typed = [i for i, u in enumerate(units) if u.typed]
    budget = policy.keep_user_chars
    for i in reversed(typed) if budget > 0 else ():  # newest first, each that still fits
        size = _message_chars(units[i].seg, read_content)
        if size > budget:
            continue  # too long for what is left: summarized; older ones may still fit
        budget -= size
        kept.add(i)
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
        elif kind == "recipient_input" and seg.content.get("rendered_content"):
            units.append(
                _Unit(
                    "user",
                    turn,
                    [seg.id],
                    head=bool(seg.content.get("human_input")),
                    typed=bool(seg.content.get("human_input")),
                    seg=seg,
                )
            )
        elif kind == "user":
            content = getattr(seg, "content", None)
            head = isinstance(content, Mapping) and "actor" not in content
            source = str(content.get("source") or "") if isinstance(content, Mapping) else ""
            steer = isinstance(content, Mapping) and content.get("actor") == "user"
            units.append(
                _Unit(
                    "user",
                    turn,
                    [seg.id],
                    head=head,
                    source=source,
                    typed=head or steer,
                    seg=seg,
                )
            )
        else:
            units.append(_Unit("summary", turn, [seg.id]))
    return units


def transcript_rows(live: Sequence[Any], ids: Sequence[str]) -> frozenset[tuple[str, str]]:
    """The transcript rows the segments ``ids`` belong to, as ``(turn id, role)``.

    A message the person sent (or its delivery record) is its turn's ``user`` row; a
    step and a summary belong to an ``assistant`` row (a summary between turns is its
    own row, keyed by the turn id it was folded under). CLIO's additions (session
    facts, plan, todos, reminders) are context, not a transcript row, so they are left
    out. A compaction stands in for a row when it folded some of that row's segments
    and kept none of them.
    """
    wanted = set(ids)
    rows = ((seg, _row_role(seg)) for seg in live if seg.id in wanted)
    return frozenset((str(getattr(seg, "turn_id", "") or ""), role) for seg, role in rows if role)


def _row_role(seg: Any) -> str:
    content = seg.content if isinstance(seg.content, Mapping) else {}
    if seg.kind == "user" and content.get("actor") == "algorithm":
        return ""
    return "user" if seg.kind in {"user", "recipient_input"} else "assistant"


def _message_chars(seg: Any, read_content: ReadContent | None) -> int:
    """The characters of text the model reads for one message (its resolved content)."""
    content = seg.content if isinstance(seg.content, Mapping) else {}
    references = content.get("rendered_content")
    if not references:
        return _text_chars(content)
    if read_content is None:
        from clio_agent.gact.agents.context_errors import ContextReadError  # noqa: PLC0415

        raise ContextReadError(
            str(getattr(seg, "scope", "")),
            ValueError("Confirmed input needs its Core content resolver"),
        )
    from clio_agent.arc.task_types import CoreReference  # noqa: PLC0415

    return sum(_text_chars(read_content(CoreReference.model_validate(r))) for r in references)


def _text_chars(content: Any) -> int:
    text = content.get("text") if isinstance(content, Mapping) else None
    return len(text) if isinstance(text, str) else len(str(content))


def _turn_order(units: Sequence[_Unit], open_turn_id: str) -> list[str]:
    """The finished turns in live order (the open turn excluded)."""
    order: list[str] = []
    for unit in units:
        if unit.kind != "summary" and unit.turn_id != open_turn_id and unit.turn_id not in order:
            order.append(unit.turn_id)
    return order
