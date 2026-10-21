"""Defined subagents and prompt files.

A defined subagent ships in the ``subagents/`` folder of the plugin it belongs
to and arrives or leaves with that plugin. Its prompt is composed from the
plugins it is given, the same way the main prompt is, plus its own prompt::

    reviewer = Subagent(
        "process-reviewer",
        prompt=PromptFile("prompt/reviewer.md"),
        plugins=["manufacturing", "pandas"],
    )

General subagents, launched at runtime with only a task prompt, come from the
``delegation`` plugin and need no definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from clio_agent.sdk.model import Prefers
from clio_agent.sdk.versions import PluginRef


@dataclass(frozen=True)
class PromptFile:
    """A prompt read from a file relative to the plugin's ``ai.iowarp.clio/`` folder."""

    path: str


@dataclass(frozen=True)
class Subagent:
    """A subagent defined by a plugin.

    ``tools`` is an optional allowlist; every name must be provided by the
    subagent's plugins. ``description`` tells the delegating agent when to use it.
    """

    id: str
    prompt: PromptFile | str = ""
    plugins: list[str] = field(default_factory=list)
    description: str = ""
    tools: list[str] | None = None
    prefers: Prefers | None = None
    flow: str = "react"
    max_iters: int = 0
    structured_outputs: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        """The declaration as data, read by the loader."""

        prompt: dict[str, str]
        if isinstance(self.prompt, PromptFile):
            prompt = {"file": self.prompt.path}
        else:
            prompt = {"text": str(self.prompt or "")}
        return {
            "id": self.id,
            "description": self.description,
            "prompt": prompt,
            "plugins": [str(PluginRef.parse(ref)) for ref in self.plugins],
            "tools": list(self.tools) if self.tools is not None else None,
            "prefers": (self.prefers or Prefers()).to_dict(),
            "flow": self.flow,
            "max_iters": int(self.max_iters or 0),
            "structured_outputs": dict(self.structured_outputs or {}),
        }
