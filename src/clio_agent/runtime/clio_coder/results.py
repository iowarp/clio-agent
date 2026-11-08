"""The hand-back record of one delegation: outcome, answer, accounting, diff and evidence."""

from __future__ import annotations

import shlex
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: Normalized outcomes. ``succeeded`` only means Clio Coder finished; ``verified`` is separate.
OUTCOME_SUCCEEDED = "succeeded"
OUTCOME_FAILED = "failed"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOME_CANCELED = "canceled"
OUTCOME_COST_CAPPED = "cost_capped"

#: ``clio-coder run`` exit codes (``clio-coder run --help``).
_EXIT_OUTCOMES = {
    0: OUTCOME_SUCCEEDED,
    4: OUTCOME_COST_CAPPED,
    124: OUTCOME_TIMED_OUT,
    143: OUTCOME_CANCELED,
}


def outcome_for_exit(exit_code: int | None) -> str:
    # No code, or a negative one (killed by a signal), means the run was stopped from outside.
    if exit_code is None or exit_code < 0:
        return OUTCOME_CANCELED
    return _EXIT_OUTCOMES.get(exit_code, OUTCOME_FAILED)


def _fact_total(fact: Any) -> float | None:
    """Collapse a Clio Coder usage fact (``measured``/``estimated``/``unknownCount``) to a number."""
    if isinstance(fact, (int, float)):
        return fact
    if isinstance(fact, dict):
        if "fact" in fact:
            return _fact_total(fact["fact"])
        if "measured" in fact or "estimated" in fact:
            return float(fact.get("measured", 0)) + float(fact.get("estimated", 0))
    return None


def summarize_usage(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Flatten Clio Coder usage (headless ``agent_end`` or ACP ``clio-coder/usage``) for reports."""
    if not usage:
        return {}
    quantities = usage.get("quantities", usage)
    summary: dict[str, Any] = {}
    for key in ("input", "output", "cacheRead", "cacheWrite", "reasoning", "totalTokens"):
        value = _fact_total(quantities.get(key))
        if value is not None:
            summary[key] = int(value)
    cost = quantities.get("cost") or usage.get("cost")
    if isinstance(cost, dict):
        usd = cost.get("usd", cost)
        summary["costUsd"] = _fact_total(usd)
        if isinstance(usd, dict) and usd.get("unknownCount"):
            summary["costUnpricedCalls"] = usd["unknownCount"]
    if "apiCalls" in usage:
        summary["apiCalls"] = usage["apiCalls"]
    return summary


@dataclass
class Verification:
    """The caller's own check of the delegated change, run in the worktree after hand-back."""

    command: list[str]
    exit_code: int | None
    output_tail: str

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


def command_text(argv: Sequence[str], *, platform: str | None = None) -> str:
    """The shell text of a check: the command itself when it is wrapped in the platform shell."""
    argv = list(argv)
    if argv[:2] == ["/bin/sh", "-c"] and len(argv) == 3:
        return argv[2]
    if argv[:4] == ["cmd", "/d", "/s", "/c"] and len(argv) == 5:
        return argv[4]
    if len(argv) == 4 and argv[1] == "-B" and Path(argv[2]).name == "windows_check.py":
        return argv[3]
    return (
        subprocess.list2cmdline(argv)
        if (platform or sys.platform).startswith("win")
        else shlex.join(argv)
    )


@dataclass
class ToolActivity:
    name: str
    status: str
    title: str = ""


@dataclass
class DelegationResult:
    """Everything the parent agent needs to judge, land or discard delegated work."""

    surface: str
    outcome: str
    exit_code: int | None = None
    outcome_detail: str | None = None
    session_id: str | None = None
    text: str = ""
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)
    tools: list[ToolActivity] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    workspace: str | None = None
    branch: str | None = None
    base_commit: str | None = None
    commit: str | None = None
    changed_files: list[str] = field(default_factory=list)
    diff: str = ""
    verification: Verification | None = None
    #: Clio Coder's own runs of the acceptance check (``turn_end.verify``); informative only,
    #: the parent's ``verification`` decides acceptance.
    child_checks: list[dict[str, Any]] = field(default_factory=list)
    confinement: dict[str, Any] = field(default_factory=dict)
    stderr_tail: str = ""
    error: dict[str, Any] | None = None
    #: The resumable run this turn belongs to, its 1-based turn number, and the usage of all
    #: its turns so far (``usage`` is this turn alone).
    run_id: str | None = None
    turn: int | None = None
    run_usage: dict[str, Any] = field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.verification is not None and self.verification.passed

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["verified"] = self.verified
        return data
