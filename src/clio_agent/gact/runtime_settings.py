"""Curated global preferences persisted through the shared user config owner."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StrictFloat, StrictInt

from clio_agent import conf
from clio_agent.arc import history_mode
from clio_agent.gact.context_preferences_types import DEFAULT_AUTOCOMPACT_PCT
from clio_agent.user_config_document import (
    USER_CONFIG_LOCK,
    read_document,
    user_config_path,
    write_document,
)

SettingValue = StrictBool | StrictInt | StrictFloat
Source = Literal["workspace", "user", "environment", "default"]


class RuntimeSettingsError(ValueError):
    """A rejected configuration update with a safe, user-facing explanation."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SettingDefinition:
    """One supported preference and its actual runtime semantics."""

    key: str
    env: str
    title: str
    description: str
    group: str
    default: bool | float
    effect: str
    minimum: float | None = None
    maximum: float | None = None
    integer: bool = False
    unit: str | None = None


_DEFINITIONS = (
    SettingDefinition(
        "limits.lm_call_s",
        "CLIO_MAX_LM_CALL_S",
        "Model request timeout",
        "Maximum time for a model request. Provider limits may end a request sooner.",
        "Execution",
        1800.0,
        "Applies to subsequent model requests.",
        minimum=0,
        unit="seconds",
    ),
    SettingDefinition(
        "limits.lm_inter_token_idle_s",
        "CLIO_LM_INTER_TOKEN_IDLE_S",
        "Stream idle timeout",
        "Maximum wait between pieces of a streamed response.",
        "Execution",
        120.0,
        "Applies to subsequent model requests.",
        minimum=0,
        unit="seconds",
    ),
    SettingDefinition(
        "tools.mcp.call_timeout_s",
        "CLIO_MCP_CALL_TIMEOUT_S",
        "Default tool timeout",
        "Used when a tool call has no explicit timeout. A tool can declare its own budget.",
        "Execution",
        600.0,
        "Restart the connected CLIO service to update existing tool clients.",
        minimum=0,
        unit="seconds",
    ),
    SettingDefinition(
        "limits.lm_transient_retries",
        "CLIO_LM_TRANSIENT_RETRIES",
        "Model retries",
        "Retries a temporary provider failure before any response has streamed.",
        "Execution",
        2.0,
        "Restart the connected CLIO service to update existing model clients.",
        minimum=0,
        integer=True,
    ),
    SettingDefinition(
        "autocompact.pct",
        "CLIO_AUTOCOMPACT_PCT",
        "Automatic compaction threshold",
        "Context fullness at which CLIO prepares a summary. Session preferences take precedence.",
        "Memory & history",
        DEFAULT_AUTOCOMPACT_PCT,
        "Applies to sessions without their own compaction threshold.",
        minimum=0,
        maximum=1,
        unit="percent",
    ),
    SettingDefinition(
        "runtime.capture_reasoning",
        "CLIO_CAPTURE_REASONING",
        "Save model reasoning",
        "Keep reasoning supplied by the provider with the saved response, when available.",
        "Memory & history",
        True,
        "Applies when subsequent responses are saved.",
    ),
    SettingDefinition(
        "transcript.file",
        "CLIO_TRANSCRIPT_FILE",
        "Save transcript files",
        "Keep a separate transcript file alongside conversation storage. Existing files are retained.",
        "Memory & history",
        True,
        "Restart the connected CLIO service for this change to take effect.",
    ),
)
_BY_KEY = {definition.key: definition for definition in _DEFINITIONS}


class RuntimeSetting(BaseModel):
    """A safe setting value, provenance, and control description for Settings."""

    key: str
    title: str
    description: str
    group: str
    value: SettingValue
    default_value: SettingValue
    kind: Literal["boolean", "number", "integer"]
    minimum: float | None
    maximum: float | None
    unit: str | None
    effect: str
    source: Source
    has_user_value: bool
    editable: bool
    reason: str | None


class RuntimeSettings(BaseModel):
    """A revisioned snapshot of global preferences, excluding private config."""

    revision: str
    config_path: str
    settings: list[RuntimeSetting]


class UpdateRuntimeSettings(BaseModel):
    """Change only named curated keys; null removes the user's override."""

    model_config = ConfigDict(extra="forbid")
    revision: str
    changes: dict[str, SettingValue | None]


def _validate(definition: SettingDefinition, value: Any) -> bool | int | float:
    if isinstance(definition.default, bool):
        if not isinstance(value, bool):
            raise RuntimeSettingsError(f"{definition.title} must be on or off.")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise RuntimeSettingsError(f"{definition.title} must be a finite number.")
    if definition.integer and value != int(value):
        raise RuntimeSettingsError(f"{definition.title} must be a whole number.")
    if definition.minimum is not None and value < definition.minimum:
        raise RuntimeSettingsError(f"{definition.title} must be at least {definition.minimum:g}.")
    if definition.minimum == 0 and not definition.integer and value == 0:
        raise RuntimeSettingsError(f"{definition.title} must be greater than zero.")
    if definition.maximum is not None and value > definition.maximum:
        raise RuntimeSettingsError(f"{definition.title} must be at most {definition.maximum:g}.")
    return int(value) if definition.integer else float(value)


def _contains(document: dict[str, Any], key: str) -> bool:
    node: Any = document
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _snapshot(document: dict[str, Any]) -> RuntimeSettings:
    path = user_config_path()
    store = conf.store()
    rows = []
    for definition in _DEFINITIONS:
        cast = conf.as_bool if isinstance(definition.default, bool) else conf.as_float
        value = _validate(
            definition,
            store.resolve(
                definition.key,
                env=definition.env,
                default=definition.default,
                cast=cast,
            ),
        )
        source = store.source(definition.key, env=definition.env)
        reason = (
            "Set by the connected service's workspace configuration."
            if source == "workspace"
            else None
        )
        if definition.key == "transcript.file" and history_mode.active():
            reason = "Transcript files are required while this service uses in-memory conversation history."
        rows.append(
            RuntimeSetting(
                key=definition.key,
                title=definition.title,
                description=definition.description,
                group=definition.group,
                value=value,
                default_value=definition.default,
                kind="boolean"
                if isinstance(definition.default, bool)
                else "integer"
                if definition.integer
                else "number",
                minimum=definition.minimum,
                maximum=definition.maximum,
                unit=definition.unit,
                effect=definition.effect,
                source=source,
                has_user_value=_contains(document, definition.key),
                editable=reason is None,
                reason=reason,
            )
        )
    # Both the full file and effective sources participate: an external edit to
    # unrelated private keys or a workspace/env override must invalidate a draft.
    raw = path.read_bytes() if path.is_file() else b""
    digest = hashlib.sha256(
        raw + json.dumps([r.model_dump() for r in rows], sort_keys=True).encode()
    ).hexdigest()
    return RuntimeSettings(revision=digest, config_path=str(path), settings=rows)


def get_runtime_settings() -> RuntimeSettings:
    """Read current defaults from the same store used by the runtime."""
    with USER_CONFIG_LOCK:
        document = read_document(user_config_path())
        conf.reload()
        return _snapshot(document)


def _set_owned_key(document: dict[str, Any], key: str, value: bool | int | float | None) -> None:
    parts = key.split(".")
    node = document
    parents: list[tuple[dict[str, Any], str]] = []
    for part in parts[:-1]:
        child = node.get(part)
        if child is None and part not in node:
            if value is None:
                return
            child = {}
            node[part] = child
        if not isinstance(child, dict):
            raise RuntimeSettingsError(
                f"The {part} configuration must be a mapping; edit the file first."
            )
        parents.append((node, part))
        node = child
    if value is None:
        node.pop(parts[-1], None)
        for parent, part in reversed(parents):
            if parent[part]:
                break
            del parent[part]
    else:
        node[parts[-1]] = value


def update_runtime_settings(request: UpdateRuntimeSettings) -> RuntimeSettings:
    """Validate and atomically save an optimistic update, preserving all other keys."""
    with USER_CONFIG_LOCK:
        document = read_document(user_config_path())
        conf.reload()
        current = _snapshot(document)
        if request.revision != current.revision:
            raise RuntimeSettingsError(
                "Configuration changed. Reload current settings before saving.", 409
            )
        rows = {row.key: row for row in current.settings}
        for key, value in request.changes.items():
            definition = _BY_KEY.get(key)
            if definition is None:
                raise RuntimeSettingsError("That configuration key is not available in Settings.")
            if not rows[key].editable:
                raise RuntimeSettingsError(rows[key].reason or "This setting cannot be edited.")
            if value is not None:
                value = _validate(definition, value)
            _set_owned_key(document, key, value)
        if request.changes:
            write_document(user_config_path(), document)
            conf.reload()
        return _snapshot(document)
