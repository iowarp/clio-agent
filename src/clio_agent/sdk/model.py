"""Model requirements and preferences.

An agent declares what its model must support (:class:`Requires`) and what it
would like (:class:`Prefers`). Plugins may add requirements of their own; the
effective requirement of a session is the union over the agent and every
resolved plugin, so removing a plugin can relax it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any


@dataclass(frozen=True)
class Requires:
    """Hard model requirements. Unset fields require nothing."""

    tool_calling: bool = False
    vision: bool = False
    context_tokens: int = 0
    output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value}

    @classmethod
    def from_dict(cls, data: Any) -> "Requires":
        if isinstance(data, Requires):
            return data
        data = dict(data or {})
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown model requirement field(s): {', '.join(unknown)}")
        return cls(**data)

    def union(self, other: "Requires") -> "Requires":
        """The stricter of both requirements, field by field."""

        return Requires(
            tool_calling=self.tool_calling or other.tool_calling,
            vision=self.vision or other.vision,
            context_tokens=max(self.context_tokens, other.context_tokens),
            output_tokens=max(self.output_tokens, other.output_tokens),
        )


@dataclass(frozen=True)
class Prefers:
    """Soft model preferences. They guide selection but never block a session."""

    model: str = ""
    provider: str = ""
    effort: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value}

    @classmethod
    def from_dict(cls, data: Any) -> "Prefers":
        if isinstance(data, Prefers):
            return data
        data = dict(data or {})
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown model preference field(s): {', '.join(unknown)}")
        return cls(**data)
