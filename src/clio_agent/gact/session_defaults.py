"""Persistent defaults applied when a client creates a session without overrides."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)

#: The only effort provenance a stored level is honoured with: the person chose
#: it. Builds before per-model levels force-wrote ``effort: "medium"`` into the
#: defaults file and into every new session's metadata without asking anyone;
#: those legacy values carry no source and are treated as unset.
EFFORT_SOURCE_USER = "user"

#: The level older builds force-wrote for everyone (never a person's choice).
LEGACY_FORCED_EFFORT = "medium"


class SessionDefaultsResponse(BaseModel):
    """What the session-defaults routes serve: the defaults plus typed degradations."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str = ""
    model_id: str = ""
    effort: str | None = None
    effort_source: str | None = None
    mode: str = "edit"
    edit_mode: str = "diff"
    routing_mode: str = "auto"
    approval_mode: str = "ask"
    blueprint_id: str = ""
    degradations: list[dict[str, str]] = Field(default_factory=list)


class SessionDefaults(BaseModel):
    """Authoritative defaults for newly created sessions."""

    model_config = ConfigDict(extra="forbid")

    provider_id: str = Field(default="", max_length=128)
    model_id: str = Field(default="", max_length=256)
    #: Starting thinking level for new sessions. ``None`` means "the selected
    #: model's own default": a fixed level here would override every model.
    effort: Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    #: ``"user"`` when a person picked ``effort``; written only by :meth:`update`.
    effort_source: Literal["user"] | None = None
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
    effort: Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    mode: Literal["plan", "edit", "architect"] | None = None
    edit_mode: Literal["diff", "whole", "patch"] | None = None
    routing_mode: Literal["auto", "chat", "experts", "reasoning_only"] | None = None
    approval_mode: Literal["ask", "auto-edits", "bypass", "ai-review", "spotter-ai"] | None = None
    blueprint_id: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_model_reference_pair(self) -> "UpdateSessionDefaultsRequest":
        """Require provider and model identifiers to change as one reference."""

        provider_present = "provider_id" in self.model_fields_set
        model_present = "model_id" in self.model_fields_set
        if provider_present != model_present:
            raise ValueError("provider_id and model_id must be updated together")
        if provider_present and bool((self.provider_id or "").strip()) != bool(
            (self.model_id or "").strip()
        ):
            raise ValueError("provider_id and model_id must both be set or both be empty")
        return self


class SessionDefaultsStore:
    """Thread-safe, atomically persisted session-default registry."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._load_degradation: dict[str, str] | None = None
        self._migration: dict[str, str] | None = None
        self._value = self._load()
        if self._value.effort is not None and self._value.effort_source != EFFORT_SOURCE_USER:
            self._migrate_legacy_effort()

    def _migrate_legacy_effort(self) -> None:
        """One-time migration of an effort written without provenance.

        Older builds force-wrote ``"medium"`` for everyone, so a sourceless
        ``"medium"`` is that default, not a choice: it is cleared (the model's
        own default applies) and reported as a typed degradation. Any OTHER
        sourceless level could only have come from a person and is kept, now
        marked user-sourced. The file is rewritten either way.
        """
        effort = self._value.effort
        if effort == LEGACY_FORCED_EFFORT:
            self._migration = {
                "reason": "session_defaults_legacy_effort_cleared",
                "effort": str(effort),
                "description": (
                    "a default reasoning effort of 'medium' written by an older build "
                    "(not chosen by anyone) was cleared; new sessions use the model default"
                ),
            }
            logger.warning("session defaults: reason=session_defaults_legacy_effort_cleared")
            self._value = self._value.model_copy(update={"effort": None})
        else:
            self._value = self._value.model_copy(update={"effort_source": EFFORT_SOURCE_USER})
        self._flush()

    @property
    def degradations(self) -> list[dict[str, str]]:
        """Typed load/migration facts a client should be able to see."""
        return [row for row in (self._load_degradation, self._migration) if row]

    def _load(self) -> SessionDefaults:
        if self._path is None or not self._path.exists():
            return SessionDefaults()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            return SessionDefaults.model_validate(payload)
        except (OSError, json.JSONDecodeError, ValueError):
            quarantine_path = self._quarantine_corrupt_file()
            self._load_degradation = {
                "reason": "session_defaults_corrupt",
                "source_path": str(self._path),
                "quarantine_path": str(quarantine_path) if quarantine_path else "",
            }
            return SessionDefaults()

    def _quarantine_corrupt_file(self) -> Path | None:
        """Move an unreadable defaults file aside without replacing prior evidence."""

        if self._path is None or not self._path.exists():
            return None
        quarantine = self._path.with_name(f"{self._path.name}.corrupt-{uuid4().hex}")
        try:
            self._path.replace(quarantine)
        except OSError:
            return None
        return quarantine

    @property
    def load_degradation(self) -> dict[str, str] | None:
        """Return typed evidence when persisted defaults could not be loaded."""

        return dict(self._load_degradation) if self._load_degradation is not None else None

    def get(self) -> SessionDefaults:
        """Return an immutable snapshot of the current defaults."""

        with self._lock:
            return self._value.model_copy(deep=True)

    def update(self, patch: UpdateSessionDefaultsRequest) -> SessionDefaults:
        """Apply a validated partial update and persist it atomically."""

        updates = patch.model_dump(exclude_none=True, exclude_unset=True)
        if "effort" in patch.model_fields_set:
            # A level here is the person's pick; an explicit null resets to the
            # selected model's own default.
            updates["effort"] = patch.effort
            updates["effort_source"] = EFFORT_SOURCE_USER if patch.effort else None
        with self._lock:
            self._value = self._value.model_copy(update=updates)
            self._flush()
            return self._value.model_copy(deep=True)

    def clear_model_ref(self) -> SessionDefaults:
        """Clear the provider/model pair after the active provider changes."""

        with self._lock:
            self._value = self._value.model_copy(update={"provider_id": "", "model_id": ""})
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


def apply_default_effort(metadata: dict[str, object], defaults: SessionDefaults) -> None:
    """Stamp a new session's starting level and its provenance into ``metadata``.

    A level the caller put in the create request is an explicit pick; otherwise
    the user-chosen default applies. Either way it is marked
    ``effort_source: "user"``; no level means the model's own default.
    """

    if not {"effort", "thinking_level"} & set(metadata) and defaults.effort:
        metadata["effort"] = defaults.effort
    if metadata.get("effort") or metadata.get("thinking_level"):
        metadata.setdefault("effort_source", EFFORT_SOURCE_USER)


def session_effort(metadata: Mapping[str, object]) -> str | None:
    """A session's starting level, only when a person chose it.

    Legacy sessions carry a force-written ``effort`` with no source; read-time
    that is treated as unset so the selected model's default applies.
    """

    if metadata.get("effort_source") != EFFORT_SOURCE_USER:
        return None
    value = metadata.get("effort") or metadata.get("thinking_level")
    return str(value) if value else None
