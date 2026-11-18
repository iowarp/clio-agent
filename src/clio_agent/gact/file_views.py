"""Client-rendered file views: what the person sees of a file only their browser can draw.

A file view is a workspace file shown by a renderer that draws it in the browser, such as
the 3D view of a STEP, STL, 3MF, OBJ, PLY or glTF file (ledger FV-D1). The server cannot
render those pixels, so the browser supplies them, as it does for an app view (CM1, CM3):

* While the view is mounted, its surface says so with ``PUT .../file-views/{view_id}``
  ``{holder, shown, file, state}``: the file's identity (workspace, path, name, the kind of
  view) and the view state as readable lines (camera, section, selection, measurements).
  The table below mirrors which file views are on screen, like the app look table; nothing
  is persisted (no new store, FV-D6).
* ``look_at_view`` looks at a file view as it looks at an app view: the session event
  :data:`FILE_VIEW_LOOK_EVENT` asks the holding surface for a snapshot, posted to
  ``POST .../file-views/{view_id}/looks/{call_id}`` (:mod:`clio_agent.gact.file_view_routes`).
* A region comment on a file view is a ``comment_session`` attachment of kind
  :data:`FILE_VIEW_KIND`, rendered by :mod:`clio_agent.gact.documents.file_view_comments`.
* While a file view is on screen, its state joins the turn's working state
  (:func:`file_view_context_injection`, ``clio.reminder.file_view_context``).

Every limit is a typed refusal (:class:`FileViewInvalid`), never a silent cut.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "FILE_VIEW_CONTEXT_SOURCE",
    "FILE_VIEW_KIND",
    "FILE_VIEW_LOOK_EVENT",
    "MAX_STATE_LINES",
    "MAX_STATE_LINE_CHARS",
    "FileView",
    "FileViewInvalid",
    "FileViewTable",
    "file_view_context_injection",
    "file_view_looks",
    "file_view_table",
    "publish_file_view_look",
    "state_lines",
    "view_file",
]

#: The comment attachment kind and the look target kind.
FILE_VIEW_KIND = "file-view"
#: The session event a surface answers with a snapshot of the file view it holds.
FILE_VIEW_LOOK_EVENT = "file_view_look"
#: The injection source and core reminder (``clio.reminder.file_view_context``).
FILE_VIEW_CONTEXT_SOURCE = "file_view_context"
#: The view state a renderer reports, at most.
MAX_STATE_LINES = 64
MAX_STATE_LINE_CHARS = 600
#: File views a session remembers (the newest; one not on screen is dropped first).
MAX_VIEWS_PER_SESSION = 32
_VIEW_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
_MAX_FIELD_CHARS = {"workspace_id": 200, "path": 4096, "name": 400, "kind": 80}


class FileViewInvalid(ValueError):
    """A file view report or comment the server refuses, with its typed reason."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


def state_lines(value: Any) -> list[str]:
    """A renderer's view state as validated lines.

    Raises:
        FileViewInvalid: ``file_view_state_invalid`` (not a list of strings) or
            ``file_view_state_too_large`` (over the line count or a line's length).
    """
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(line, str) for line in value):
        raise FileViewInvalid("file_view_state_invalid", "the view state must be a list of lines")
    if len(value) > MAX_STATE_LINES:
        raise FileViewInvalid(
            "file_view_state_too_large",
            f"the view state has {len(value)} lines; at most {MAX_STATE_LINES} are accepted",
        )
    longest = max((len(line) for line in value), default=0)
    if longest > MAX_STATE_LINE_CHARS:
        raise FileViewInvalid(
            "file_view_state_too_large",
            f"a view state line has {longest} characters; at most {MAX_STATE_LINE_CHARS}",
        )
    return [" ".join(line.split()) for line in value if line.strip()]


def view_file(value: Any) -> dict[str, str]:
    """The file a view shows: ``{workspace_id, path, name, kind}``.

    Raises:
        FileViewInvalid: ``file_view_identity_missing`` (no path) or
            ``file_view_identity_invalid`` (a field that is not text or is too long).
    """
    row = value if isinstance(value, Mapping) else {}
    out: dict[str, str] = {}
    for key, limit in _MAX_FIELD_CHARS.items():
        item = row.get(key, "")
        if not isinstance(item, str) or len(item) > limit:
            raise FileViewInvalid(
                "file_view_identity_invalid", f"file.{key} must be text of at most {limit} chars"
            )
        out[key] = item.strip()
    if not out["path"]:
        raise FileViewInvalid("file_view_identity_missing", "the file view names no file path")
    out["name"] = out["name"] or out["path"].rsplit("/", 1)[-1]
    out["kind"] = out["kind"] or "file"
    return out


def check_view_id(view_id: str) -> str:
    """A view id as a surface names it (letters, digits, ``_`` and ``-``).

    Raises:
        FileViewInvalid: ``file_view_id_invalid``.
    """
    if not _VIEW_ID.fullmatch(view_id or ""):
        raise FileViewInvalid("file_view_id_invalid", "a view id is 1 to 80 letters or digits")
    return view_id


@dataclass
class FileView:
    """One client-rendered file view of a session, as its surfaces last reported it."""

    view_id: str
    file: dict[str, str]
    state: list[str] = field(default_factory=list)
    order: int = 0

    @property
    def name(self) -> str:
        return self.file.get("name", "")

    @property
    def path(self) -> str:
        return self.file.get("path", "")

    @property
    def kind(self) -> str:
        return self.file.get("kind", "file")


class FileViewTable:
    """Per session: the file views surfaces reported, newest last, and their state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._views: dict[str, dict[str, FileView]] = {}
        self._order = 0

    def report(
        self, sid: str, view_id: str, file: dict[str, str], state: list[str] | None
    ) -> FileView:
        """Record what a surface reported; ``state`` ``None`` keeps the last state."""
        with self._lock:
            views = self._views.setdefault(sid, {})
            self._order += 1
            view = views.get(view_id)
            if view is None:
                view = views[view_id] = FileView(view_id, dict(file))
            view.file = dict(file)
            view.order = self._order
            if state is not None:
                view.state = list(state)
            return view

    def prune(self, sid: str, shown: set[str]) -> None:
        """Keep at most :data:`MAX_VIEWS_PER_SESSION` views, dropping the oldest unshown."""
        with self._lock:
            views = self._views.get(sid, {})
            idle = sorted((v for v in views.values() if v.view_id not in shown), key=_order)
            while len(views) > MAX_VIEWS_PER_SESSION and idle:
                views.pop(idle.pop(0).view_id, None)

    def get(self, sid: str, view_id: str) -> FileView | None:
        with self._lock:
            return self._views.get(sid, {}).get(view_id)

    def views(self, sid: str) -> list[FileView]:
        with self._lock:
            return sorted(self._views.get(sid, {}).values(), key=_order)


def _order(view: FileView) -> int:
    return view.order


def file_view_table(app: Any) -> FileViewTable:
    """The app's file view table (created on first use)."""
    state = app.state
    table = getattr(state, "file_views", None)
    if not isinstance(table, FileViewTable):
        table = FileViewTable()
        state.file_views = table
    return table


def file_view_looks(app: Any) -> Any:
    """Which surfaces hold each file view, and the looks waiting on them: the app look
    table's own type, keyed by view id (one implementation of holders and waiting)."""
    from clio_agent.gact.mcp_app_look import ViewLooks  # noqa: PLC0415

    state = app.state
    table = getattr(state, "file_view_looks", None)
    if not isinstance(table, ViewLooks):
        table = ViewLooks()
        state.file_view_looks = table
    return table


def publish_file_view_look(app: Any, look: Any) -> None:
    """Ask the surface holding the file view for a snapshot of it."""
    from clio_agent.gact.events import Event  # noqa: PLC0415
    from clio_agent.gact.mcp_app_look import LOOK_MAX_SIDE  # noqa: PLC0415

    bus = getattr(app.state, "bus", None)
    if bus is not None:
        bus.publish(
            Event(
                type=FILE_VIEW_LOOK_EVENT,
                session_id=look.session_id,
                payload={
                    "session_id": look.session_id,
                    "view_id": look.app_instance_id,
                    "call_id": look.call_id,
                    "max_side": LOOK_MAX_SIDE,
                },
                transient=True,
            )
        )


def shown_views(app: Any, sid: str) -> list[FileView]:
    """The session's file views a surface holds now."""
    looks = file_view_looks(app)
    return [view for view in file_view_table(app).views(sid) if looks.shown(sid, view.view_id)]


def describe(view: FileView, *, shown: bool) -> str:
    """How the agent is told which file view is meant."""
    where = "on screen" if shown else "not on screen"
    return f"“{view.name}” ({view.kind} view of the workspace file {view.path}, {where})"


def not_shown_text(view: FileView) -> str:
    """Why a file view cannot be looked at, and how the person shows it."""
    return (
        f"The file view {describe(view, shown=False)} is not on screen, so it cannot be "
        "looked at. The person can show it by opening the file from Files in the workbench, "
        "or by bringing its tab forward."
    )


def file_view_context_injection(app: Any, sid: str) -> str:
    """The turn's block for every file view on screen, with its view state."""
    from clio_agent import injections  # noqa: PLC0415

    blocks = []
    for view in shown_views(app, sid):
        text = injections.render(
            FILE_VIEW_CONTEXT_SOURCE,
            {
                "path": view.path,
                "kind": view.kind,
                "state": "\n".join(view.state[:MAX_STATE_LINES]),
            },
            app=app,
            session_id=sid,
        )
        if text:
            blocks.append(text)
    return "\n\n".join(blocks)


def state_words(lines: Sequence[str]) -> list[str]:
    """The view state lines as the agent reads them (already validated)."""
    return [str(line) for line in lines][:MAX_STATE_LINES]
