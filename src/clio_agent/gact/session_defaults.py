"""Persistent defaults applied when a client creates a session without overrides."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SessionDefaults(BaseModel):
    """Authoritative defaults for newly created sessions."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(default="", max_length=128)
    model_id: str = Field(default="", max_length=256)
    effort: Literal["off", "low", "medium", "high"] = "medium"
    mode: Literal["plan", "edit", "architect"] = "edit"
    edit_mode: Literal["diff", "whole", "patch"] = "diff"
    routing_mode: Literal["auto", "chat", "experts", "reasoning_only"] = "auto"
    approval_mode: Literal["ask", "auto-edits", "bypass", "ai-review", "spotter-ai"] = "ask"
    blueprint_id: str = Field(default="", max_length=512)


class UpdateSessionDefaultsRequest(BaseModel):
    """Partial replacement accepted by ``PATCH /v1/session-defaults``."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str | None = Field(default=None, max_length=128)
    model_id: str | None = Field(default=None, max_length=256)
    effort: Literal["off", "low", "medium", "high"] | None = None
    mode: Literal["plan", "edit", "architect"] | None = None
    edit_mode: Literal["diff", "whole", "patch"] | None = None
    routing_mode: Literal["auto", "chat", "experts", "reasoning_only"] | None = None
    approval_mode: Literal["ask", "auto-edits", "bypass", "ai-review", "spotter-ai"] | None = None
    blueprint_id: str | None = Field(default=None, max_length=512)


class SessionDefaultsStore:
    """Thread-safe, atomically persisted session-default registry."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._value = self._load()

    def _load(self) -> SessionDefaults:
        if self._path is None or not self._path.exists():
            return SessionDefaults()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            return SessionDefaults.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValueError):
            return SessionDefaults()

    def get(self) -> SessionDefaults:
        """Return an immutable snapshot of the current defaults."""

        with self._lock:
            return self._value.model_copy(deep=True)

    def update(self, patch: UpdateSessionDefaultsRequest) -> SessionDefaults:
        """Apply a validated partial update and persist it atomically."""

        updates = patch.model_dump(exclude_none=True, exclude_unset=True)
        with self._lock:
            self._value = self._value.model_copy(update=updates)
            self._flush()
            return self._value.model_copy(deep=True)

    def _flush(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(self._path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(self._value.model_dump_json(indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self._path)
