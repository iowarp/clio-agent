"""The background pass's consolidate stage: proposes memory changes, never writes."""

from clio_agent.sdk import PromptFile, Subagent

curator = Subagent(
    "curator",
    prompt=PromptFile("prompt/curator.md"),
    description="Turns candidate facts from a conversation into proposed memory changes.",
    plugins=["memory"],
    tools=["memory_search", "memory_read", "propose_memory_change"],
    internal=True,
)
