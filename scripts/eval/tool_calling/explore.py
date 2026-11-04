"""Exploration of real scientific data: a plain request on a dataset, read by hand.

``X1`` is the OPAL phenotyping export (``appl-vision``: imaging modalities, plant
weights and a gallery per acquisition day) that prompted the long-horizon work: the
owner's request there was to explore it and understand what it holds and how it is
shaped. The request names no tool and no step. The data is not part of the generated
fixtures: ``CLIO_EVAL_OPAL_DATA`` names its folder (default below), and each run works
in a fresh copy of it, as for every task. There is no answer key; a run is read, with
its per-run report, against the Claude Code reference on the same copy.
"""

from __future__ import annotations

import os
from pathlib import Path

from scripts.eval.tool_calling.tasks import Task

DEFAULT_OPAL = Path("/home/user/opal-data/appl-vision")
PROMPTS = {
    "X1": "Explore this dataset and understand what it contains and how it is shaped.",
}

#: X2: the owner's exp67 QY_max chain (2026-11-04), answered from the files. Round 2
#: needs round 1's peak and round 3 needs round 2's dose; each round has a trap
#: (the avg_QY_max look-alike, a plain dip, absolute instead of relative retention,
#: plant instead of cell means). This export blanks missing readings (null) where the
#: chain's source used 1.797e+308, so the setup names both.
_X2_SETUP = (
    "This folder holds the export of OPAL experiment 67: a nickel dose series on "
    "pennycress (Thlaspi arvense) at 0, 50, 100, 200, 500 and 1000 mg Ni/kg (the "
    "treatment), 12 accessions (genotypes) x 5 replicates, imaged over about a month. "
    "QY_max is the maximum quantum yield of photosystem II from the fc1 chlorophyll "
    "fluorescence modality; fc1 also has a look-alike variable, avg_QY_max, which is "
    "not the same measure. Missing readings may be empty or encoded as 1.797e+308.\n\n"
)
_X2_ROUNDS = (
    "At which nickel dose does mean QY_max peak across the panel? Take each plant's "
    "mean over its readings, then the mean of those per dose.",
    "Above that peak dose, at which dose does QY_max first decline significantly "
    "(p < 0.05, Welch t-test against the dose just below it)?",
    "Which three accessions tolerate 500 mg/kg best, ranked by retention: each "
    "accession's mean QY_max at 500 divided by its own mean at 0?",
    "What is the unweighted mean of the 12 accessions' QY_max at each dose, that is the "
    "mean of the accession (cell) means rather than of the plants?",
)
_ANSWER_LINE = " End your reply with one line: ANSWER: <your answer>."
#: Rounds 1-3 from the chain's gold; round 4 from this export's cell means (the
#: chain's source gives values about 0.015-0.025 lower, from a different extract).
X2_KEY = (
    ("200",),
    ("500",),
    ("ISU 210", "9", "RIL 407"),
    (0.7434, 0.7552, 0.7414, 0.7905, 0.6598, 0.6593),
)
CHAINS = {"X2": (_X2_SETUP, _X2_ROUNDS)}


def is_explore_task(task_id: str) -> bool:
    return task_id in PROMPTS or task_id in CHAINS


def explore_task(task_id: str) -> Task:
    if task_id in CHAINS:
        setup, rounds = CHAINS[task_id]
        asked = [question + _ANSWER_LINE for question in rounds]
        return Task(task_id, "long-horizon", setup + asked[0], rounds=tuple(asked[1:]))
    return Task(task_id, "long-horizon", PROMPTS[task_id])


def grade_x2(replies: list[str]) -> dict[str, object]:
    """Each X2 round against its key: doses and the ranked names in order, the six
    means within 0.002 each."""
    import re  # noqa: PLC0415

    from scripts.eval.tool_calling.chains import answer_line  # noqa: PLC0415

    rounds = []
    for index, key in enumerate(X2_KEY):
        line = answer_line(replies[index]) if index < len(replies) else ""
        if isinstance(key[0], float):
            got = [float(n) for n in re.findall(r"\d+\.\d+", line)]
            ok = len(got) == len(key) and all(abs(a - b) <= 0.002 for a, b in zip(got, key, strict=True))
        elif len(key) == 1:
            ok = re.findall(r"\d+", line)[:1] == list(key)
        else:
            names = [part.strip(" .*`") for part in re.split(r",|;|\n", line) if part.strip()]
            ok = [n.lower() for n in names[: len(key)]] == [k.lower() for k in key]
        rounds.append({"round": index + 1, "answer": line, "correct": ok})
    right = sum(r["correct"] for r in rounds)
    return {"correct": right == len(X2_KEY), "round_recall": round(right / len(X2_KEY), 3),
            "rounds": rounds}  # fmt: skip


def fixture(task_id: str) -> str:
    """The data folder, absolute (a fixtures root joined with it yields it)."""
    del task_id
    folder = Path(os.environ.get("CLIO_EVAL_OPAL_DATA") or DEFAULT_OPAL)
    if not folder.is_dir():
        raise FileNotFoundError(f"OPAL data folder {folder} not found (CLIO_EVAL_OPAL_DATA)")
    return str(folder.resolve())
