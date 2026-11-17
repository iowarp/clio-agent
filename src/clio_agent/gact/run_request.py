"""The run fields of ``POST /v1/sessions``: a flow to run, unattended, and limits.

A script (the Python library, ``clio-agent -q``) creates its session with these:

* ``flow`` — the session runs this flow as its own work: the first message is the
  flow's input and each later message starts a new run. Checked before the session
  exists (:func:`clio_agent.gact.flow_tools.flow_refusal`, shared with ``run_flow``);
  it is the only way to set the session's ``agent_flow``.
* ``unattended`` — nobody is watching (:mod:`clio_agent.gact.unattended`).
* ``max_turns`` / ``max_budget_usd`` — limits over the session's whole tree
  (:mod:`clio_agent.gact.run_limits`).
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class FlowRequest(BaseModel):
    """The flow a new session runs as its own work."""

    id: str = Field(min_length=1)
    options: dict[str, Any] = Field(default_factory=dict)


class RunRequestFields(BaseModel):
    """Fields a session-creating script sets; every session accepts them."""

    flow: Optional[FlowRequest] = None
    unattended: bool = False
    max_turns: Optional[int] = Field(default=None, ge=1)
    max_budget_usd: Optional[float] = Field(default=None, gt=0)

    def run_limits(self) -> dict[str, Any]:
        """The limits this request sets (empty when none)."""

        return {
            key: value
            for key, value in (
                ("max_turns", self.max_turns),
                ("max_budget_usd", self.max_budget_usd),
            )
            if value is not None
        }


__all__ = ["FlowRequest", "RunRequestFields"]
