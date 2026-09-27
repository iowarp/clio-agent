"""The provider support a person installed, recorded so a runtime change cannot lose it.

Optional provider support (the Claude Agent SDK behind Claude Code, the Globus
SDK behind ALCF sign-in) and in-place provider SDK updates are installed into
the ACTIVE backend environment. That environment is not durable: a CLIO Desktop
update replaces its whole bundled runtime, and a reinstall or ``uv tool
upgrade`` rebuilds a tool environment. Anything installed there afterwards is
gone the next time CLIO starts.

So every install and every in-place component update is also recorded here, in
the user configuration file's ``providers.installed_support`` section (the
EXISTING ``config.yaml`` that :mod:`clio_agent.conf` reads; no new store)::

    providers:
      installed_support:
        claude_code:
          versions:
            claude-agent-sdk: 0.2.159

A provider kind listed here is support the person asked for; ``versions`` are
floors (the newest version an in-place update installed). At startup
:mod:`clio_agent.providers.support_restore` compares the record with the
running environment and reinstalls what is missing. Removing an entry by hand
stops CLIO from restoring it.

A failure to write the record is never swallowed: it is logged with the typed
reason :data:`PROVIDER_SUPPORT_NOT_RECORDED` and kept for the restore status
route (:func:`last_record_failure`), because it means the support will NOT
survive the next update.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml
from packaging.version import InvalidVersion, Version

from clio_agent import user_config_document

logger = logging.getLogger(__name__)

#: Where the record lives inside ``config.yaml``.
SECTION = ("providers", "installed_support")

#: Typed reason: an install succeeded but could not be recorded.
PROVIDER_SUPPORT_NOT_RECORDED = "provider_support_not_recorded"

#: Typed reason: the record exists but cannot be read (malformed ``config.yaml``).
PROVIDER_SUPPORT_RECORD_UNREADABLE = "provider_support_record_unreadable"

_FAILURE_LOCK = threading.Lock()
_LAST_FAILURE: dict[str, "RecordOutcome"] = {}


@dataclass(frozen=True)
class RecordOutcome:
    """The result of one record attempt (``reason`` is empty on success)."""

    provider_kind: str
    persisted: bool
    path: str
    reason: str = ""
    detail: str = ""

    def to_wire(self) -> dict[str, Any]:
        """JSON shape used by the restore status route."""
        return {
            "provider_kind": self.provider_kind,
            "persisted": self.persisted,
            "path": self.path,
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RecordRead:
    """The recorded support (``reason`` set when the record could not be read)."""

    entries: dict[str, dict[str, str]]
    path: str
    reason: str = ""
    detail: str = ""


def _newer(a: str, b: str) -> str:
    """The newer of two version strings (an unparsable one never wins)."""
    try:
        return a if Version(a) >= Version(b) else b
    except InvalidVersion:
        return b if _parses(b) else a


def _parses(value: str) -> bool:
    try:
        Version(value)
    except InvalidVersion:
        return False
    return True


def _entries_of(document: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """``{kind: {distribution: floor}}`` from a config document (tolerates absence)."""
    section: Any = document
    for part in SECTION:
        section = section.get(part) if isinstance(section, Mapping) else None
    if isinstance(section, list):  # a hand-written plain list of kinds
        return {str(kind): {} for kind in section if str(kind).strip()}
    if not isinstance(section, Mapping):
        return {}
    entries: dict[str, dict[str, str]] = {}
    for kind, body in section.items():
        versions = body.get("versions") if isinstance(body, Mapping) else None
        entries[str(kind)] = (
            {str(name): str(version) for name, version in versions.items() if version}
            if isinstance(versions, Mapping)
            else {}
        )
    return entries


def read_recorded_support() -> RecordRead:
    """Read the recorded provider support from the user ``config.yaml``."""

    path = user_config_document.user_config_path()
    try:
        with user_config_document.USER_CONFIG_LOCK:
            document = user_config_document.read_document(path)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        logger.warning(
            "⚑ PROVIDER-SUPPORT reason=%s path=%s detail=%s",
            PROVIDER_SUPPORT_RECORD_UNREADABLE,
            path,
            exc,
        )
        return RecordRead({}, str(path), PROVIDER_SUPPORT_RECORD_UNREADABLE, str(exc))
    return RecordRead(_entries_of(document), str(path))


def record_support(provider_kind: str, versions: Mapping[str, str] | None = None) -> RecordOutcome:
    """Record that ``provider_kind``'s support was installed (at least ``versions``).

    Read-modify-write under the shared user-config lock with an atomic replace;
    every other key of ``config.yaml`` is preserved. A recorded floor only moves
    up. Idempotent: recording what is already recorded rewrites nothing.

    Returns:
        :class:`RecordOutcome`; on failure ``reason`` is
        :data:`PROVIDER_SUPPORT_NOT_RECORDED` (also logged and kept for
        :func:`last_record_failure`).
    """

    path = user_config_document.user_config_path()
    try:
        with user_config_document.USER_CONFIG_LOCK:
            document = user_config_document.read_document(path)
            entries = _entries_of(document)
            current = dict(entries.get(provider_kind, {}))
            merged = dict(current)
            for name, version in (versions or {}).items():
                if version:
                    merged[name] = _newer(version, merged[name]) if name in merged else version
            if provider_kind not in entries or merged != current:
                entries[provider_kind] = merged
                providers = document.get(SECTION[0])
                section = dict(providers) if isinstance(providers, Mapping) else {}
                section[SECTION[1]] = {
                    kind: ({"versions": floors} if floors else {})
                    for kind, floors in sorted(entries.items())
                }
                document[SECTION[0]] = section
                user_config_document.write_document(path, document)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        outcome = RecordOutcome(
            provider_kind, False, str(path), PROVIDER_SUPPORT_NOT_RECORDED, str(exc)
        )
        logger.warning(
            "⚑ PROVIDER-SUPPORT reason=%s provider=%s path=%s detail=%s",
            PROVIDER_SUPPORT_NOT_RECORDED,
            provider_kind,
            path,
            exc,
        )
        with _FAILURE_LOCK:
            _LAST_FAILURE[provider_kind] = outcome
        return outcome
    with _FAILURE_LOCK:
        _LAST_FAILURE.pop(provider_kind, None)
    logger.info("provider support recorded provider=%s versions=%s", provider_kind, versions or {})
    return RecordOutcome(provider_kind, True, str(path))


def last_record_failure() -> list[RecordOutcome]:
    """Record attempts that failed and have not since succeeded, per provider kind."""
    with _FAILURE_LOCK:
        return list(_LAST_FAILURE.values())


def reset_record_failures() -> None:
    """Forget remembered record failures (test isolation)."""
    with _FAILURE_LOCK:
        _LAST_FAILURE.clear()


__all__ = [
    "PROVIDER_SUPPORT_NOT_RECORDED",
    "PROVIDER_SUPPORT_RECORD_UNREADABLE",
    "SECTION",
    "RecordOutcome",
    "RecordRead",
    "last_record_failure",
    "read_recorded_support",
    "record_support",
    "reset_record_failures",
]
