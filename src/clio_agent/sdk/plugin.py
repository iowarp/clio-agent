"""``Plugin`` and ``Pack``: identity, requirements and membership.

``ai.iowarp.clio/plugin.py`` carries identity, requirements and metadata only.
Everything else a plugin contributes is found by convention in its folders.
A plugin is declared either as a subclass or as an instance::

    class Manufacturing(Plugin):
        id = "manufacturing"
        version = "1.2.0"
        requires = ["shell", "a2a"]
        model = Requires(context_tokens=64_000)

    documents = Pack("documents", ["pdf", "docx", "xlsx", "pptx"])

A pack is a plugin whose content is only membership in other plugins. Pack
membership is removable; ``requires`` is a hard requirement whose removal also
removes the plugin that declares it.
"""

from __future__ import annotations

from typing import Any, ClassVar

from clio_agent.sdk.model import Prefers, Requires
from clio_agent.sdk.versions import PluginRef


class Plugin:
    """A plugin's identity and requirements."""

    id: ClassVar[str] = ""
    version: ClassVar[str] = ""
    requires: ClassVar[list[str]] = []
    model: ClassVar[Requires | None] = None
    prefers: ClassVar[Prefers | None] = None
    kind: ClassVar[str] = "plugin"

    def __init__(
        self,
        id: str | None = None,  # noqa: A002 - the design's public field name
        *,
        version: str | None = None,
        requires: list[str] | None = None,
        model: Requires | None = None,
        prefers: Prefers | None = None,
    ) -> None:
        # Instance form overrides the class attributes; the class form needs no call.
        if id is not None:
            self.__dict__["id"] = id
        if version is not None:
            self.__dict__["version"] = version
        if requires is not None:
            self.__dict__["requires"] = list(requires)
        if model is not None:
            self.__dict__["model"] = model
        if prefers is not None:
            self.__dict__["prefers"] = prefers

    def describe(self) -> dict[str, Any]:
        """The declaration as data, read by the loader."""

        return {
            "id": str(self.id or ""),
            "version": str(self.version or ""),
            "kind": self.kind,
            "requires": [str(PluginRef.parse(ref)) for ref in (self.requires or [])],
            "members": [],
            "model": (self.model or Requires()).to_dict(),
            "prefers": (self.prefers or Prefers()).to_dict(),
        }


class Pack(Plugin):
    """A plugin whose content is only membership in other plugins."""

    members: ClassVar[list[str]] = []
    kind: ClassVar[str] = "pack"

    def __init__(
        self,
        id: str | None = None,  # noqa: A002 - the design's public field name
        members: list[str] | None = None,
        *,
        version: str | None = None,
        requires: list[str] | None = None,
    ) -> None:
        super().__init__(id, version=version, requires=requires)
        if members is not None:
            self.__dict__["members"] = list(members)

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data["members"] = [str(PluginRef.parse(ref)) for ref in (self.members or [])]
        return data


def declaration_of(value: Any) -> Plugin | None:
    """Return the :class:`Plugin` declared by ``value`` (a subclass or an instance)."""

    if isinstance(value, Plugin):
        return value
    if isinstance(value, type) and issubclass(value, Plugin) and value not in (Plugin, Pack):
        return value()
    return None
