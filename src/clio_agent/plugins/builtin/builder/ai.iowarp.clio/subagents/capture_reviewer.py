"""Reads a plugin the builder wrote cold, without the session, as a stranger would."""

from clio_agent.sdk import PromptFile, Subagent

capture_reviewer = Subagent(
    "capture-reviewer",
    prompt=PromptFile("prompt/capture-reviewer.md"),
    description=(
        "Reviews a plugin just written from a session, without seeing the session: reports "
        "what a stranger would get wrong (a description that will not trigger, a value "
        "still specific to one run, a fragment over budget, a hook that blocks too much). "
        "Give it the plugin's name and every file's text, not the session."
    ),
    effects="read",
)
