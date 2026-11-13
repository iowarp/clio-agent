"""A user agent saved from the + menu: a parent agent plus a delta.

Moved out of :mod:`clio_agent.plugins.store` (which re-exports it) so the store stays
under the file-size cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["UserAgent"]


@dataclass(frozen=True)
class UserAgent:
    """A user agent: a parent agent plus a delta, never a flattened list."""

    id: str
    name: str
    extends: str
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "extends": self.extends,
            "add": list(self.add),
            "remove": list(self.remove),
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserAgent":
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            extends=str(data["extends"]),
            add=tuple(str(v) for v in data.get("add") or ()),
            remove=tuple(str(v) for v in data.get("remove") or ()),
            description=str(data.get("description") or ""),
        )
