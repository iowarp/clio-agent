"""Generic atomic, 0600, schema-tagged JSON keyed-file store.

Extracted from :mod:`clio_agent.tools.mcp_oauth_storage` (#1285) so a second
durable credential store — the direct Codex subscription provider
(:mod:`clio_agent.providers.codex.credentials`) — does not duplicate the
create-at-0600 + atomic-replace dance. Both stores keep their own schema tag
and their own per-entry shape; this module owns only "read the whole file",
"atomically replace the whole file", and the file-permission discipline.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from clio_agent.runtime import trace

__all__ = ["AtomicJsonFileStore"]


class AtomicJsonFileStore:
    """One JSON file shaped ``{"schema": <tag>, "entries": {...}}``.

    Args:
        path: The backing file.
        schema: The exact schema tag every read must match; a mismatch (or an
            unreadable/corrupt file) degrades to an empty store rather than
            raising — the caller re-populates it on its next write.
        trace_tag: The :func:`clio_agent.runtime.trace.event` tag warnings are
            logged under (e.g. ``"TOOLS"``, ``"PROVIDERS"``).
    """

    def __init__(self, path: Path, *, schema: str, trace_tag: str = "TOOLS") -> None:
        self._path = path
        self._schema = schema
        self._trace_tag = trace_tag

    @property
    def path(self) -> Path:
        return self._path

    def read_entries(self) -> dict[str, dict[str, object]]:
        """Return the stored ``entries`` map, or ``{}`` on any read/schema failure."""

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            trace.event(
                self._trace_tag, "atomic_json_store_unreadable path=%s reason=%s", self._path, exc
            )
            return {}
        if not isinstance(raw, dict) or raw.get("schema") != self._schema:
            trace.event(
                self._trace_tag,
                "atomic_json_store_schema_mismatch path=%s got=%s want=%s",
                self._path,
                raw.get("schema") if isinstance(raw, dict) else type(raw).__name__,
                self._schema,
            )
            return {}
        entries = raw.get("entries")
        return entries if isinstance(entries, dict) else {}

    def write_entries(self, entries: dict[str, dict[str, object]]) -> None:
        """Atomically replace the file's contents, created at 0600 from the moment it exists.

        The temp file is opened with ``os.O_CREAT`` at mode ``0o600`` directly
        (never a default-mode create followed by a later ``chmod``), so no
        window opens where a partially-written credential bundle is readable
        at a wider mode. ``os.replace`` preserves that mode on POSIX; the
        trailing ``chmod`` re-asserts it defensively (e.g. an unusual
        filesystem that doesn't preserve mode across a rename) and is itself
        never a silent failure.
        """

        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = json.dumps({"schema": self._schema, "entries": entries}, indent=1)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, self._path)
        try:
            os.chmod(self._path, 0o600)
        except OSError as exc:
            trace.event(
                self._trace_tag, "atomic_json_store_chmod_failed path=%s reason=%s", self._path, exc
            )
