"""The evaluation tasks: plain requests, their fixture folder and session setup.

Every prompt is a plain request that names no tool and no steps (the plan's
"Evaluation" section). A prompt may name a function or a file, because that
is the user's question, not a method. P5 to P10 add one ``clio-eval`` plugin
to the standard agent through the session's plugin delta.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scripts.eval.tool_calling.paths import MARKETPLACE_NAME

STANDARD_AGENT = "clio/standard"
#: Lowered compaction threshold for H3 and H5 (fraction of the context window).
H3_AUTOCOMPACT_PCT = 0.25


@dataclass(frozen=True)
class Task:
    """One evaluation task."""

    id: str
    suite: str  # "performance" or "long-horizon"
    prompt: str
    plugins: tuple[str, ...] = ()
    #: Extra session metadata (H3's and H5's compaction threshold).
    metadata: dict[str, Any] = field(default_factory=dict)

    def agent_delta(self) -> dict[str, list[str]]:
        return {"add": [f"{MARKETPLACE_NAME}/{name}" for name in self.plugins], "remove": []}


def _p1(answer: dict[str, Any]) -> str:
    return (
        f"Where is the function `{answer['function']}` defined in this source tree, and "
        "where is it called from? Give the file and line of the definition and of every call."
    )


def _p2(answer: dict[str, Any]) -> str:
    return (
        f"In `{answer['file']}`, what clock drift tolerance (in ppm) applies to active "
        "stations, and on which line is it set?"
    )


def _p4(answer: dict[str, Any]) -> str:
    names = list(answer["subsystems"])
    listed = ", ".join(f"`{n}`" for n in names[:-1]) + f" and `{names[-1]}`"
    return (
        f"Survey this repository's three subsystems, {listed} under "
        f"`{answer['package_root']}`: for each, what it does, its main modules and its "
        "entry points."
    )


_RECORDS = "Which records have a negative reading, and how many?"
_H1 = "The test suite in this repository has started failing. Find out why."
_H2 = (
    "Reading records with this package has become much slower than it used to be. "
    "Why is this I/O path slower than it was?"
)
#: A broad investigation of a data export, the shape of a real one over 139 files and
#: 1.24 GB; it names no file, tool, step or finding.
_H4 = (
    "This folder is an export of experimental data. Investigate it broadly: what was "
    "done, what the data shows, and whether anything is wrong with it."
)

#: Static prompts; P1, P2 and P4 are filled from the manifest's answer keys.
_STATIC: dict[str, Task] = {
    "P3": Task(
        "P3",
        "performance",
        "Review this package and list the five riskiest issues with file and line.",
    ),
    "P5": Task("P5", "performance", _RECORDS, ("eval-records-20",)),
    "P6": Task("P6", "performance", _RECORDS, ("eval-records-200",)),
    "P7": Task("P7", "performance", _RECORDS, ("eval-records-2000",)),
    "P8": Task("P8", "performance", "Set the guest bedroom thermostat to 19.5 °C.", ("eval-home",)),
    "P9": Task("P9", "performance", "Water the back garden beds for ten minutes.", ("eval-home",)),
    "P10": Task(
        "P10",
        "performance",
        "In two sentences, what is the README in this folder about?",
        ("eval-home",),
    ),
    "H1": Task("H1", "long-horizon", _H1),
    "H2": Task("H2", "long-horizon", _H2),
    "H4": Task("H4", "long-horizon", _H4),
}
#: Tasks that rerun another with the compaction threshold lowered, so they compact.
_COMPACTING = {"H3": _H1, "H5": _H4}
_FILLED = {"P1": _p1, "P2": _p2, "P4": _p4}
ALL_TASKS = (
    "P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10", "H1", "H2", "H3", "H4", "H5",
)  # fmt: skip
#: Tasks the Claude Code reference runs.
REFERENCE_TASKS = ("P1", "P2", "P3", "P4")


def task(
    task_id: str, manifest: dict[str, Any], *, h3_autocompact_pct: float = H3_AUTOCOMPACT_PCT
) -> Task:
    """The task ``task_id`` with its prompt filled from ``manifest``.

    H3 and H5 are H1 and H4 with automatic compaction at ``h3_autocompact_pct``.
    """
    if task_id in _FILLED:
        answer = manifest["tasks"][task_id]["answer"]
        return Task(task_id, "performance", _FILLED[task_id](answer))
    if task_id in _COMPACTING:
        return Task(
            task_id,
            "long-horizon",
            _COMPACTING[task_id],
            metadata={
                "context_preferences": {
                    "automatic_compaction": True,
                    "autocompact_pct": h3_autocompact_pct,
                }
            },
        )
    if task_id not in _STATIC:
        raise KeyError(f"unknown task {task_id!r}; expected one of {', '.join(ALL_TASKS)}")
    return _STATIC[task_id]


def select(spec: str) -> list[str]:
    """Parse ``P1,P3-P5,H*`` style task selections, in catalog order."""
    chosen: set[str] = set()
    for part in (s.strip().upper() for s in spec.split(",") if s.strip()):
        if part in ("ALL", "*"):
            chosen.update(ALL_TASKS)
        elif part.endswith("*"):
            chosen.update(t for t in ALL_TASKS if t.startswith(part[:-1]))
        elif "-" in part:
            low, high = part.split("-", 1)
            ids = list(ALL_TASKS)
            chosen.update(ids[ids.index(low) : ids.index(high) + 1])
        elif part in ALL_TASKS:
            chosen.add(part)
        else:
            raise KeyError(f"unknown task {part!r}")
    return [t for t in ALL_TASKS if t in chosen]


def fixture_dir(task_id: str, manifest: dict[str, Any]) -> str:
    """The fixture folder (relative to the fixtures root) a task works in."""
    return str(manifest["tasks"][task_id]["fixture"])
