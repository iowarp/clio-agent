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


def is_explore_task(task_id: str) -> bool:
    return task_id in PROMPTS


def explore_task(task_id: str) -> Task:
    return Task(task_id, "long-horizon", PROMPTS[task_id])


def fixture(task_id: str) -> str:
    """The data folder, absolute (a fixtures root joined with it yields it)."""
    del task_id
    folder = Path(os.environ.get("CLIO_EVAL_OPAL_DATA") or DEFAULT_OPAL)
    if not folder.is_dir():
        raise FileNotFoundError(f"OPAL data folder {folder} not found (CLIO_EVAL_OPAL_DATA)")
    return str(folder.resolve())
