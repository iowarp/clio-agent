"""Multi-turn science chains: each OPAL-style chain is one session, a round per turn.

The chains come from the attention-provenance suite's public chain set
(``spotter-ai-genesis/experiments``, branch ``opal-poisoning-suite``,
``bench/opal/data/opal_chains.csv``): 100 chains of 2 to 7 rounds about synthetic
phenotyping experiments, each round a task with its tool outputs given inline and a
gold answer. A chain runs as one session: round 1 is the request, and each later round
is sent when the previous turn has ended (``Task.rounds``), so the agent carries the
session's rules, tables and earlier answers across turns and compactions.

What they are for: a probe of the turn stack (request to agent to provider to model),
not a test. Every round is one model call on inlined tool outputs, so a chain shows
CLIO's per-turn cost plainly (bring-up phases, provider handoff) and says nothing about
long tool use or coherence on a strong model (42 of 42 rounds correct on
``claude-sonnet-5-5``, ledger finding 14). Run one chain when measuring that path, not
batches.

Task ids are ``C1`` to ``C100`` in the CSV's chain order (``CLIO_EVAL_CHAINS`` names
another copy of the file). They are not part of ``all``. They need no files and run in
the ``home`` fixture.

Each round asks for one ``ANSWER: <value>`` line. A round is graded on that line (or
the reply's last line when it has none) against the gold answer: every ``;`` part must
match, numbers by value, comma lists as sets, text ignoring case and spacing. The chain
is correct when every round is.
"""

from __future__ import annotations

import csv
import os
import re
from functools import cache
from pathlib import Path
from typing import Any

from scripts.eval.tool_calling.tasks import Task

DEFAULT_CSV = Path("/home/user/spotter-ai-genesis/experiments/bench/opal/data/opal_chains.csv")
#: The folder a chain runs in (it reads no files).
FIXTURE = "home"
_ANSWER = re.compile(r"^\W*ANSWER\W*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def csv_path() -> Path:
    return Path(os.environ.get("CLIO_EVAL_CHAINS") or DEFAULT_CSV)


@cache
def _chains(path: str) -> list[tuple[str, list[dict[str, str]]]]:
    by_chain: dict[str, list[dict[str, str]]] = {}
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            by_chain.setdefault(row["chain_id"], []).append(row)
    return [(cid, sorted(rows, key=lambda r: int(r["round_no"]))) for cid, rows in by_chain.items()]


def chains() -> list[tuple[str, list[dict[str, str]]]]:
    """``(chain_id, rounds)`` in the CSV's order."""
    return _chains(str(csv_path()))


def is_chain_task(task_id: str) -> bool:
    return bool(re.fullmatch(r"C\d+", task_id))


def chain_rows(task_id: str) -> tuple[str, list[dict[str, str]]]:
    index = int(task_id[1:]) - 1
    found = chains()
    if not 0 <= index < len(found):
        raise KeyError(f"unknown chain task {task_id!r}; expected C1 to C{len(found)}")
    return found[index]


def chain_task(task_id: str) -> Task:
    """The chain as a task: round 1 is the request, later rounds follow each turn."""
    chain_id, rows = chain_rows(task_id)
    return Task(
        task_id,
        "multi-turn",
        rows[0]["prompt"],
        metadata={"chain_id": chain_id},
        rounds=tuple(r["prompt"] for r in rows[1:]),
    )


def answer_line(reply: str) -> str:
    """The reply's ``ANSWER:`` value (the last one), else its last non-empty line."""
    found = _ANSWER.findall(reply)
    if found:
        return found[-1].strip()
    lines = [line.strip() for line in reply.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _part(text: str) -> Any:
    """One ``;`` part, comparable: a number, a set of comma items, or folded text."""
    text = " ".join(text.replace("−", "-").strip(" .`*").lower().split())
    try:
        return float(text)
    except ValueError:
        pass
    if "," in text:
        return frozenset(item.strip() for item in text.split(",") if item.strip())
    return text


def round_correct(gold: str, reply: str) -> bool:
    """Whether ``reply`` answers ``gold``: each ``;`` part equal, numbers by value and
    comma lists as sets."""
    given = answer_line(reply).split(";")
    wanted = gold.split(";")
    return len(given) == len(wanted) and all(
        _part(a) == _part(b) for a, b in zip(given, wanted, strict=True)
    )


def grade_chain(task_id: str, replies: list[str]) -> dict[str, Any]:
    """Each round against its gold answer, from the agent's final reply to that round."""
    chain_id, rows = chain_rows(task_id)
    graded = []
    for i, row in enumerate(rows):
        reply = replies[i] if i < len(replies) else ""
        graded.append(
            {
                "round": int(row["round_no"]),
                "task_type": row["task_type"],
                "gold": row["correct_answer"],
                "answered": bool(reply),
                "correct": bool(reply) and round_correct(row["correct_answer"], reply),
            }
        )
    right = sum(r["correct"] for r in graded)
    return {
        "chain_id": chain_id,
        "correct": right == len(graded),
        "round_recall": round(right / len(graded), 3),
        "rounds": graded,
    }
