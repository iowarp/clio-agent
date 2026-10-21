"""The CLIO Agent SDK.

Plugins hold all content. Agents hold only their flow, their identity, their
plugin selection and their model requirements. Python is the authoring
surface; the frozen spec produced at install is the runtime contract.

See ``docs/design/sdk/agent-sdk.md`` and ``plugins-and-composition.md``.
"""

from clio_agent.sdk.agent import Agent, AgentDefinitionError, AgentSpec
from clio_agent.sdk.flow import Flow
from clio_agent.sdk.hooks import HOOK_EVENTS, HookEvent, hook
from clio_agent.sdk.model import Prefers, Requires
from clio_agent.sdk.plan import Node, Plan, PlanError
from clio_agent.sdk.plugin import Pack, Plugin
from clio_agent.sdk.subagent import PromptFile, Subagent
from clio_agent.sdk.tools import native_tool, tool
from clio_agent.sdk.versions import PluginRef

__all__ = [
    "HOOK_EVENTS",
    "Agent",
    "AgentDefinitionError",
    "AgentSpec",
    "Flow",
    "HookEvent",
    "Node",
    "Pack",
    "Plan",
    "PlanError",
    "Plugin",
    "PluginRef",
    "Prefers",
    "PromptFile",
    "Requires",
    "Subagent",
    "hook",
    "native_tool",
    "tool",
]
