"""Delegate coding work to a Clio Coder instance.

Clio Coder is a separate coding agent (a Node CLI). clio-agent drives it as a child process
through one of two surfaces it ships:

* ``clio-coder run --json-events terminal``: one headless turn, one authoritative terminal frame.
* ``clio-coder acp``: an Agent Client Protocol v1 server over stdio for multi-turn sessions with
  streamed tool progress, mode/model selection, mediated permission requests and cancel.

Every instance runs with its own scoped Clio Coder home (config/data/state/cache) under a
caller-owned state root, never the operator's real home, and works in a disposable git worktree
whose diff is handed back to the caller together with the run's outcome and accounting.
An ACP delegation can stay open as a run that later calls continue, and that CLIO lands into
the checkout (or discards) once its acceptance check passes.
"""

from clio_agent.runtime.clio_coder.delegate import (
    ContinueRequest,
    DelegationRequest,
    continue_run,
    delegate_coding_task,
)
from clio_agent.runtime.clio_coder.drivers import RunOptions
from clio_agent.runtime.clio_coder.landing import LandResult, land_run
from clio_agent.runtime.clio_coder.launch import (
    ClioCoderError,
    ClioCoderInstall,
    ClioCoderTarget,
    InstanceHome,
)
from clio_agent.runtime.clio_coder.results import DelegationResult

__all__ = [
    "ClioCoderError",
    "ClioCoderInstall",
    "ClioCoderTarget",
    "ContinueRequest",
    "DelegationRequest",
    "DelegationResult",
    "InstanceHome",
    "LandResult",
    "RunOptions",
    "continue_run",
    "delegate_coding_task",
    "land_run",
]
