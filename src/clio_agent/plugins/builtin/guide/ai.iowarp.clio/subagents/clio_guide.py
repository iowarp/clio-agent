"""The guide's reader for broad questions: many docs pages, read in its own context."""

from clio_agent.sdk import Prefers, PromptFile, Subagent

clio_guide = Subagent(
    "clio-guide",
    prompt=PromptFile("prompt/clio-guide.md"),
    description=(
        "Answers a broad question about using CLIO (how to set something up, which "
        "features fit a goal) from the docs of this version, reading as many pages as it "
        "needs. Returns the answer, the pages it used, and the steps and screenshots that "
        "belong in the reply. Give it the person's question in their words."
    ),
    prefers=Prefers(role="guide"),
    effects="read",
)
