"""Delegate coding work to a Clio Coder instance.

Clio Coder is a separate coding agent (a Node CLI). clio-agent drives it as a child process
through one of two surfaces it ships:

* ``clio-coder run --json-events terminal``: one headless turn, one authoritative terminal frame.
* ``clio-coder acp``: an Agent Client Protocol v1 server over stdio for multi-turn sessions with
  streamed tool progress, mode/model selection, mediated permission requests and cancel.

Every instance runs with its own scoped Clio Coder home (config/data/state/cache) under a
caller-owned state root, never the operator's real home, and works in a disposable git worktree
whose diff is handed back to the caller together with the run's outcome and accounting.
"""

from clio_agent.runtime.clio_coder.delegate import DelegationRequest, delegate_coding_task
from clio_agent.runtime.clio_coder.drivers import RunOptions
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
    "DelegationRequest",
    "DelegationResult",
    "InstanceHome",
    "RunOptions",
    "delegate_coding_task",
]
