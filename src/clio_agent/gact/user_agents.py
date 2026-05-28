"""User-registered agent store (iowarp/clio-agent#19).

Mirrors WorkspaceStore: dataclass + threading.Lock + atomic JSON
flush. Built-ins (main, data, analysis, visualization) live in
``app.gact.app._builtin_agents`` and are NOT in this store; PUT/
DELETE on those ids is rejected at the HTTP layer.

Each row is the same wire shape as AgentDef so the user can pass
the result of GET /v1/agents back into POST /v1/agents to clone.
"""

from __future__ import annotations

import os
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


def _default_store_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / "clio-agent" / "agents.json"
    return Path.home() / ".config" / "clio-agent" / "agents.json"


@dataclass
class UserAgent:
    """One row in the registry. Mirrors gact.types.AgentDef."""

    id: str
    title: str = ""
    description: str = ""
    source: str = "user"
    system_prompt: str = ""
    default_provider: str = ""
    default_model: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)
    tier: int = 2
    specialization: str = ""
    keywords: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    capability_refs: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)


class UserAgentStore:
    """Thread-safe registry with optional JSON persistence."""

    def __init__(self, *, path: Optional[Path] = None) -> None:
        self._lock = threading.Lock()
        self._path = path
        self._agents: dict[str, UserAgent] = {}
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            import json

            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        for row in data.get("agents", []):
            try:
                self._agents[row["id"]] = UserAgent(
                    **{
                        k: row[k]
                        for k in (
                            "id",
                            "title",
                            "description",
                            "source",
                            "system_prompt",
                            "default_provider",
                            "default_model",
                            "tier",
                            "specialization",
                        )
                        if k in row
                    }
                    | {
                        "parameters": dict(row.get("parameters", {})),
                        "keywords": list(row.get("keywords", [])),
                        "tools": list(row.get("tools", [])),
                        "skills": list(row.get("skills", [])),
                        "commands": list(row.get("commands", [])),
                        "capability_refs": list(row.get("capability_refs", [])),
                        "metadata": dict(row.get("metadata", {})),
                    }
                )
            except Exception:
                continue

    def _flush(self) -> None:
        if self._path is None:
            return
        import json

        self._path.parent.mkdir(parents=True, exist_ok=True)
        rows = [a.to_wire() for a in self._agents.values()]
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"agents": rows}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self._path)

    def upsert(self, payload: dict[str, Any]) -> UserAgent:
        agent = UserAgent(
            id=payload["id"],
            title=payload.get("title", "") or payload["id"],
            description=payload.get("description", ""),
            source=payload.get("source") or "user",
            system_prompt=payload.get("system_prompt", "") or "",
            default_provider=payload.get("default_provider", "") or "",
            default_model=payload.get("default_model", "") or "",
            parameters=dict(payload.get("parameters") or {}),
            tier=int(payload.get("tier") or 2),
            specialization=payload.get("specialization", "") or "",
            keywords=list(payload.get("keywords") or []),
            tools=list(payload.get("tools") or []),
            skills=list(payload.get("skills") or []),
            commands=list(payload.get("commands") or []),
            capability_refs=list(payload.get("capability_refs") or []),
            metadata=dict(payload.get("metadata") or {}),
        )
        with self._lock:
            self._agents[agent.id] = agent
            self._flush()
        return agent

    def get(self, agent_id: str) -> Optional[UserAgent]:
        with self._lock:
            return self._agents.get(agent_id)

    def list(self) -> list[UserAgent]:
        with self._lock:
            return sorted(self._agents.values(), key=lambda a: a.id)

    def delete(self, agent_id: str) -> bool:
        with self._lock:
            existed = agent_id in self._agents
            self._agents.pop(agent_id, None)
            self._flush()
        return existed
