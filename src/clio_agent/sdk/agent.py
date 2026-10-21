"""``Agent``: a flow, an identity and a set of plugins.

Agents own only their flow, their identity (``role``), their plugin selection
and their model requirements and preferences. A new class means a new
identity; new capabilities arrive as plugins and packs::

    class StandardAgent(Agent):
        id = "standard"
        version = "1.0.0"
        role = "a general research assistant"
        plugins = Agent.plugins + ["documents", "shell", "delegation"]
        requires = Requires(tool_calling=True, context_tokens=128_000)

    class Factorio(ScienceAgent):
        id = "factorio"
        role = "a manufacturing and topology optimization agent"
        plugins = ScienceAgent.plugins + ["manufacturing@^1"]

:meth:`Agent.spec` imports nothing else: it flattens the class chain into a
frozen :class:`AgentSpec` with a checksum. That spec, stored at install, is the
runtime contract. Plugins are referenced by id with a version range, never by
import.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, ClassVar

from clio_agent.sdk.flow import Flow
from clio_agent.sdk.model import Prefers, Requires
from clio_agent.sdk.versions import PluginRef, VersionRangeError

CORE_PLUGIN = "core"


class AgentDefinitionError(ValueError):
    """An agent class does not satisfy the SDK contract."""


@dataclass(frozen=True)
class PluginSelection:
    """One plugin the agent carries, with the class level that added it."""

    ref: PluginRef
    layer: str

    def to_dict(self) -> dict[str, Any]:
        return {"ref": str(self.ref), "layer": self.layer}


@dataclass(frozen=True)
class AgentSpec:
    """The frozen, flattened definition of an agent."""

    id: str
    version: str
    name: str
    role: str
    description: str
    flow: dict[str, Any]
    plugins: tuple[PluginSelection, ...]
    layers: tuple[str, ...]
    requires: dict[str, Any] = field(default_factory=dict)
    prefers: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def body(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "version": self.version,
            "name": self.name,
            "role": self.role,
            "description": self.description,
            "flow": self.flow,
            "plugins": [selection.to_dict() for selection in self.plugins],
            "layers": list(self.layers),
            "requires": self.requires,
            "prefers": self.prefers,
            "source": self.source,
        }

    @property
    def checksum(self) -> str:
        blob = json.dumps(self.body(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "checksum": self.checksum}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AgentSpec":
        spec = cls(
            id=str(data["id"]),
            version=str(data.get("version") or ""),
            name=str(data.get("name") or ""),
            role=str(data.get("role") or ""),
            description=str(data.get("description") or ""),
            flow=dict(data.get("flow") or {"id": "react", "options": {}}),
            plugins=tuple(
                PluginSelection(PluginRef.parse(row["ref"]), str(row.get("layer") or ""))
                for row in data.get("plugins") or []
            ),
            layers=tuple(str(layer) for layer in data.get("layers") or []),
            requires=dict(data.get("requires") or {}),
            prefers=dict(data.get("prefers") or {}),
            source=str(data.get("source") or ""),
        )
        expected = str(data.get("checksum") or "")
        if expected and expected != spec.checksum:
            raise AgentDefinitionError(f"agent spec {spec.id!r} checksum mismatch")
        return spec


def _flow_selection(value: Any) -> dict[str, Any]:
    if isinstance(value, Flow):
        return value.reference()
    if isinstance(value, type) and issubclass(value, Flow):
        return {"id": value.id, "options": {}}
    text = str(value or "").strip()
    if not text:
        raise AgentDefinitionError("agent flow must name a flow id")
    return {"id": text, "options": {}}


class Agent:
    """Base class for shipped agents. The base carries only ``core``."""

    id: ClassVar[str] = ""
    version: ClassVar[str] = "0.0.0"
    name: ClassVar[str] = ""
    role: ClassVar[str] = ""
    description: ClassVar[str] = ""
    flow: ClassVar[Any] = "react"
    plugins: ClassVar[list[str]] = [CORE_PLUGIN]
    requires: ClassVar[Requires] = Requires()
    prefers: ClassVar[Prefers] = Prefers()

    @classmethod
    def spec(cls) -> AgentSpec:
        """Flatten the class chain into a frozen :class:`AgentSpec`."""

        if cls is Agent or not cls.id:
            raise AgentDefinitionError(f"{cls.__name__} must set a non-empty id")
        chain = [klass for klass in reversed(cls.__mro__) if issubclass(klass, Agent)]
        seen: dict[str, PluginSelection] = {}
        layers: list[str] = []
        for klass in chain:
            declared = klass.__dict__.get("plugins")
            if declared is None:
                continue
            layer = klass.__dict__.get("id") or ("agent" if klass is Agent else klass.__name__)
            layers.append(str(layer))
            current: dict[str, PluginSelection] = {}
            for raw in declared:
                try:
                    ref = PluginRef.parse(raw)
                except VersionRangeError as exc:
                    raise AgentDefinitionError(f"{klass.__name__}.plugins: {exc}") from exc
                if ref.id in current:
                    raise AgentDefinitionError(
                        f"{klass.__name__}.plugins lists {ref.id!r} more than once"
                    )
                previous = seen.get(ref.id)
                current[ref.id] = PluginSelection(
                    ref, previous.layer if previous is not None else str(layer)
                )
            # A subclass list replaces its parent's list: dropping a parent plugin is allowed.
            seen = current
        if CORE_PLUGIN not in seen:
            raise AgentDefinitionError(f"agent {cls.id!r} must carry the {CORE_PLUGIN!r} plugin")
        requires = cls.requires if isinstance(cls.requires, Requires) else Requires()
        prefers = cls.prefers if isinstance(cls.prefers, Prefers) else Prefers()
        return AgentSpec(
            id=str(cls.id),
            version=str(cls.version or "0.0.0"),
            name=str(cls.name or cls.id),
            role=str(cls.role or ""),
            description=str(cls.description or (cls.__doc__ or "").strip()),
            flow=_flow_selection(cls.flow),
            plugins=tuple(seen.values()),
            layers=tuple(layers),
            requires=requires.to_dict(),
            prefers=prefers.to_dict(),
            source=f"{cls.__module__}.{cls.__qualname__}",
        )
