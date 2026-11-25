"""Questions search asks the person, shared by every caller that needs the answer.

Two decisions belong to the person, never to CLIO or an agent:

* **approval** -- may CLIO send queries to a source? Asked the first time a source
  would be used (``approve:<source>``); an approval is saved in ``search.approved``.
  When several web sources are still unanswered, one question names them all in the
  order they are tried (``approve:web=<a>,<b>,...``) and its answer applies to each:
  asking them one by one meant a person who declined one source was asked about the
  next at once, five questions for one search, and a run nobody watched waited on each.
* **switching** -- a source's free allowance is used up; switch to the next one,
  always switch from now on, add a key, or stop? Asked once per exhaustion
  (``exhausted:<source>:<epoch>``) unless ``search.web.auto_switch`` is on.

:func:`gate` returns a recorded answer, waits for another caller's pending
question, or raises :class:`AskPerson` so the calling tool can return the
question (MCP input-required). The tool's retry passes the answer to
:func:`apply_answer`, which records it for every waiting caller.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from clio_agent.search import settings_store
from clio_agent.search.settings import DISPLAY_NAMES, LITERATURE_APPROVAL
from clio_agent.tools.servers.web_limits import SEARCH_LIMITS

APPROVE = "approve"
DECLINE = "decline"
SWITCH = "switch"
SWITCH_ALWAYS = "switch_always"
ADD_KEY = "add_key"
STOP = "stop"


@dataclass(frozen=True)
class Decision:
    """A question for the person: a message and its choices (value, label)."""

    topic: str
    message: str
    options: tuple[tuple[str, str], ...]
    #: The choice that proceeds: what a script run (nobody to ask) answers
    #: (:mod:`clio_agent.gact.unattended`).
    default: str = ""


@dataclass
class AskPerson(Exception):
    """This caller must ask the person; the tool returns the question."""

    decision: Decision
    extra: dict[str, str] = field(default_factory=dict)


class WaitingForPerson(Exception):
    """Another caller's question is still unanswered after this caller's patience."""


def _name(source: str) -> str:
    return DISPLAY_NAMES.get(source, source)


def approval(source: str) -> Decision:
    """The approval question for ``source``."""

    if source == LITERATURE_APPROVAL:
        what = (
            "OpenAlex, Crossref, arXiv, ChemRxiv, Europe PMC, OSTI.gov, OpenCitations and DataCite"
        )
        label = "the literature sources"
    else:
        what = _name(source)
        label = _name(source)
    return Decision(
        topic=f"approve:{source}",
        message=(
            f"Allow searches through {label}?\n\nSearch queries leave this computer and "
            f"{what} sees them. CLIO asks once for each source."
        ),
        options=((APPROVE, f"Allow searches through {label}"), (DECLINE, f"Don't use {label}")),
        default=APPROVE,
    )


#: Topic prefix of the one question for several web sources (``approve:web=a,b``).
WEB_SOURCES_TOPIC = "approve:web="


def web_approval(sources: tuple[str, ...]) -> Decision:
    """One approval question for the web ``sources`` still unanswered, in try order."""

    names = [_name(source) for source in sources]
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    return Decision(
        topic=WEB_SOURCES_TOPIC + ",".join(sources),
        message=(
            f"Allow web searches through {listed}?\n\nSearch queries leave this computer. "
            "CLIO tries these sources in this order and moves to the next when one cannot "
            "answer; each sees the queries it answers. Settings → Web search allows or turns "
            "off each one."
        ),
        options=(
            (APPROVE, "Allow these web search sources"),
            (DECLINE, "Don't search the web"),
        ),
        default=APPROVE,
    )


def _clock(epoch_seconds: float) -> str:
    return time.strftime("%H:%M", time.localtime(epoch_seconds))


def exhausted(source: str, epoch: int, until: float, next_source: str) -> Decision:
    """The switching question after ``source``'s allowance ran out."""

    nxt = _name(next_source)
    return Decision(
        topic=f"exhausted:{source}:{epoch}",
        message=(
            f"{_name(source)}'s free search allowance is used up. What should CLIO do?"
            f"\n\nIt resets around {_clock(until)}."
        ),
        options=(
            (SWITCH, f"Switch to {nxt} (free)"),
            (SWITCH_ALWAYS, f"Switch to {nxt}, and always switch from now on"),
            (ADD_KEY, f"I'll add a {_name(source)} key in Settings → Web search"),
            (STOP, "Stop searching for now"),
        ),
        default=SWITCH,
    )


async def gate(decision: Decision) -> str:
    """The person's answer to ``decision``: recorded, awaited, or asked now."""

    for _ in range(2):
        state, value = SEARCH_LIMITS.claim(decision.topic)
        if state == "answer":
            return value
        if state == "lead":
            raise AskPerson(decision)
        answer = await SEARCH_LIMITS.wait_for(decision.topic)
        if answer:
            return answer
    raise WaitingForPerson(decision.topic)


def apply_answer(topic: str, value: str) -> None:
    """Record the person's answer for ``topic`` and make it durable where it should be."""

    kind, _, rest = topic.partition(":")
    if kind == "approve":
        sources = (
            rest.removeprefix("web=").split(",") if topic.startswith(WEB_SOURCES_TOPIC) else [rest]
        )
        for source in filter(None, sources):
            if value == APPROVE:
                settings_store.approve_source(source)
            else:
                SEARCH_LIMITS.decline(source)
    elif kind == "exhausted" and value == SWITCH_ALWAYS:
        settings_store.enable_auto_switch()
    SEARCH_LIMITS.decide(topic, value)


def abandon(topic: str) -> None:
    """The question was cancelled: let the next caller ask again."""

    SEARCH_LIMITS.abandon(topic)


__all__ = [
    "ADD_KEY",
    "APPROVE",
    "DECLINE",
    "STOP",
    "SWITCH",
    "SWITCH_ALWAYS",
    "WEB_SOURCES_TOPIC",
    "AskPerson",
    "Decision",
    "WaitingForPerson",
    "abandon",
    "apply_answer",
    "approval",
    "exhausted",
    "gate",
    "web_approval",
]
