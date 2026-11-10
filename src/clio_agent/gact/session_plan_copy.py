"""A fork takes the plan and the todos with it, as its own copies.

The plan file is copied to the branch's own path, so two sessions never write one file;
the todos and the plan status are copied as they are.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from clio_agent.runtime import trace

__all__ = ["copied_working_state"]


def copied_working_state(app: Any, source: Any, branch: Any) -> dict[str, Any]:
    """The metadata patch that gives ``branch`` copies of ``source``'s plan and todos."""
    from clio_agent.gact.plan_mode import _compute_plan_file_path  # noqa: PLC0415
    from clio_agent.gact.session_plan import PLAN_STATUS_KEY  # noqa: PLC0415

    metadata = source.metadata or {}
    patch: dict[str, Any] = {}
    if metadata.get("todos"):
        patch["todos"] = list(metadata["todos"])
    plan = str(metadata.get("plan_file") or "")
    if plan and Path(plan).is_file():
        target = _compute_plan_file_path(app, branch.id, branch)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(plan, target)
        except OSError as exc:
            # The branch starts without the plan; the trace says why (no silent loss).
            trace.event("PLAN", "fork_plan_copy_failed source=%s error=%r", plan, exc)
            return patch
        patch["plan_file"] = str(target)
        if metadata.get(PLAN_STATUS_KEY):
            patch[PLAN_STATUS_KEY] = metadata[PLAN_STATUS_KEY]
    return patch
