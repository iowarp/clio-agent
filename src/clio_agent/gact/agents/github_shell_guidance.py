"""Shared GitHub instructions selected by actual shell capability, never by model."""

from __future__ import annotations

from collections.abc import Iterable

from clio_agent.prompts import PromptRegistry


def github_shell_guidance(tool_names: Iterable[str]) -> str:
    """Return the editable prompt snippet only for agents with a declared shell."""
    if not {"bash", "shell_bash"}.intersection(tool_names):
        return ""
    prompt = PromptRegistry().resolve("clio.runtime.github_shell")
    if prompt is None or not prompt.text.strip() or prompt.validation_errors:
        raise ValueError("The shared GitHub shell prompt is unavailable")
    return prompt.text.strip()
