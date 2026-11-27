"""One session's scan for /optimize: findings with evidence, run on the optimize role."""

from clio_agent.sdk import Prefers, PromptFile, Subagent

optimize_scanner = Subagent(
    "optimize-scanner",
    prompt=PromptFile("prompt/optimize-scanner.md"),
    description=(
        "Reads one session's record and returns what went wrong or was missing, with "
        "evidence, attributed to a plugin part, a gap, or something no plugin can fix. "
        "Started by the optimize-scan flow, one per session."
    ),
    tools=["session_record", "run_code", "load_skill"],
    prefers=Prefers(role="optimize"),
)
