"""The CLIO Agent SDK.

Plugins hold all content. Agents hold only their flow, their identity, their
plugin selection and their model requirements. Python is the authoring
surface; the frozen spec produced at install is the runtime contract.

See ``docs/design/sdk/agent-sdk.md`` and ``plugins-and-composition.md``.
"""

from typing import TYPE_CHECKING

from clio_agent.sdk.agent import Agent, AgentDefinitionError, AgentSpec
from clio_agent.sdk.hooks import HOOK_EVENTS, HookEvent, hook
from clio_agent.sdk.model import Prefers, Requires
from clio_agent.sdk.plugin import Pack, Plugin
from clio_agent.sdk.subagent import PromptFile, Subagent
from clio_agent.sdk.tools import native_tool, tool
from clio_agent.sdk.versions import PluginRef
from clio_agent.sdk.workflow import Step, Workflow, WorkflowError

if TYPE_CHECKING:
    from clio_agent.sdk.flow import Flow as Flow

__all__ = [
    "HOOK_EVENTS",
    "Agent",
    "AgentDefinitionError",
    "AgentSpec",
    "Flow",
    "HookEvent",
    "Pack",
    "Plugin",
    "PluginRef",
    "Prefers",
    "PromptFile",
    "Requires",
    "Step",
    "Subagent",
    "hook",
    "native_tool",
    "tool",
    "Workflow",
    "WorkflowError",
]


def __getattr__(name: str) -> object:
    # ``Flow`` is a ``dspy.Module``; it is imported on first use so that loading
    # plugin records and specs (the server's import path) does not load the engine.
    if name == "Flow":
        from clio_agent.sdk.flow import Flow  # noqa: PLC0415

        return Flow
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
