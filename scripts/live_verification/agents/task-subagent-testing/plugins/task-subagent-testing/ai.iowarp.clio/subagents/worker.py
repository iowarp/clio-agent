"""The qualification worker: a defined subagent of task-subagent-testing."""

from clio_agent.sdk import PromptFile, Subagent

worker = Subagent(
    "worker",
    prompt=PromptFile("prompt/worker.md"),
    description="Task Qualification Worker: performs the supplied assignment with real tools",
    plugins=["workspace", "shell"],
    tools=["shell_bash", "fs_read_file"],
)
