"""The agent looks at an open app view: its pixels and its last reported context (CM1).

``look_at_view`` is a native tool. It names one of the session's live MCP App views (by
title, server, tool or instance id; the only one when there is one) and returns an
image of what that view shows right now, as the person sees it, with the context the view
last reported (:mod:`clio_agent.gact.mcp_app_context`) and the view's identity.

The pixels come from the surface that holds the view, as an app tool's answer does
(MA13): the tool publishes the session event :data:`LOOK_EVENT`; the surface asks CLIO's
sandbox document for a snapshot of the view
(:mod:`clio_agent.gact.mcp_app_snapshot_script`) and posts it to
``POST .../mcp-apps/{app_id}/looks/{call_id}``. The call waits with no timeout (rule 6);
cancelling the turn releases it. A surface says it holds a view through
``PUT .../mcp-apps/{app_id}/shown``; a view no surface holds is answered at once, naming
how to show it.

The image reaches the model as the call's native image part: the result is a
``view_image`` descriptor (:mod:`clio_agent.gact.view_image_tool`) whose ``model_text`` is
the readable identity, context and coverage (:mod:`clio_agent.gact.agents.recorded_media`).
Every gap is typed: a view not shown (``mcp_app_view_not_shown``), several views and no
choice (``mcp_app_view_ambiguous``), no view (``mcp_app_view_none``), a snapshot the
sandbox refused (its own reason), and the parts of the view the snapshot could not paint.

The pending looks and the holders are runtime state that mirrors which views are on
screen, like the app tool table; nothing here is persisted.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import secrets
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from clio_agent.runtime import trace

__all__ = [
    "LOOK_EVENT",
    "LookAnswerInvalid",
    "ViewLooks",
    "build_look_at_view_tool",
    "decode_snapshot_png",
    "look_table",
    "publish_look",
    "view_context_lines",
]

#: The session event a surface answers with a snapshot of the view it holds.
LOOK_EVENT = "mcp_app_look"
#: The longest side of the image the agent receives, in pixels.
LOOK_MAX_SIDE = 1568
#: A snapshot's encoded PNG, at most (the base64 text the surface posts).
MAX_SNAPSHOT_BASE64 = 16 * 1024 * 1024
#: Typed outcomes.
VIEW_NOT_SHOWN = "mcp_app_view_not_shown"
VIEW_NONE = "mcp_app_view_none"
VIEW_AMBIGUOUS = "mcp_app_view_ambiguous"
VIEW_CLOSED = "mcp_app_view_closed"
#: How much of a view's context the agent reads with one look or one comment.
CONTEXT_CHARS = 4000

_SKIP_WORDS = {
    "frame_cross_origin": "a frame from another origin",
    "webgl_frame_not_redrawn": "a WebGL canvas that did not redraw when asked",
    "webgl_canvas_idle": "a blank WebGL canvas that drew nothing while the view redrew",
    "cross_origin_pixels": "pixels from another origin",
    "not_drawable": "an element the browser could not draw",
    "wrapped_text_not_drawn": "text that wraps over several lines",
}


class LookAnswerInvalid(ValueError):
    """A surface's answer that is not a snapshot (the route answers 422)."""


@dataclass
class PendingLook:
    """One look waiting for the surface's snapshot."""

    call_id: str
    session_id: str
    app_instance_id: str
    done: threading.Event = field(default_factory=threading.Event)
    answer: dict[str, Any] | None = None
    closed: bool = False

    def event_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "app_instance_id": self.app_instance_id,
            "call_id": self.call_id,
            "max_side": LOOK_MAX_SIDE,
        }


class ViewLooks:
    """Per session: which surface holds each view, and the looks waiting on views."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # Every mount that holds the view now (a card and a tab can overlap while a frame
        # moves); the view is shown while any holds it.
        self._holders: dict[tuple[str, str], list[str]] = {}
        self._looks: dict[str, PendingLook] = {}

    def set_shown(self, sid: str, app_id: str, holder: str, shown: bool) -> bool:
        """A mount of the view loaded (``shown``) or unloaded; returns whether any mount
        still holds the view. An unload names its own mount, so a frame moving between the
        card and a tab never hides the other mount; an empty ``holder`` lets every mount go.
        """
        key = (sid, app_id)
        with self._lock:
            holders = self._holders.setdefault(key, [])
            if holder in holders:
                holders.remove(holder)
            if shown:
                holders.append(holder)
            elif not holder:
                holders.clear()
            if not holders:
                self._holders.pop(key, None)
                return False
            return True

    def shown(self, sid: str, app_id: str) -> bool:
        with self._lock:
            return (sid, app_id) in self._holders

    def begin(self, sid: str, app_id: str) -> PendingLook:
        look = PendingLook(f"look_{secrets.token_urlsafe(12)}", sid, app_id)
        with self._lock:
            self._looks[look.call_id] = look
        return look

    def pending_for_app(self, sid: str, app_id: str) -> list[PendingLook]:
        with self._lock:
            return [
                look
                for look in self._looks.values()
                if look.session_id == sid and look.app_instance_id == app_id
            ]

    def settle(self, sid: str, app_id: str, call_id: str, answer: Mapping[str, Any]) -> bool:
        """Deliver the surface's snapshot; ``False`` when no look of this app waits."""
        with self._lock:
            look = self._looks.get(call_id)
            if look is None or look.session_id != sid or look.app_instance_id != app_id:
                return False
            self._looks.pop(call_id, None)
        look.answer = dict(answer)
        look.done.set()
        return True

    def forget(self, look: PendingLook) -> None:
        with self._lock:
            self._looks.pop(look.call_id, None)

    def drop_app(self, sid: str, app_id: str) -> None:
        """The app closed: no surface holds it, and every waiting look learns so."""
        with self._lock:
            self._holders.pop((sid, app_id), None)
            looks = self.pending_for_app(sid, app_id)
            for look in looks:
                self._looks.pop(look.call_id, None)
        for look in looks:
            look.closed = True
            look.done.set()


def look_table(app: Any) -> ViewLooks:
    """The app's table (created on first use)."""
    state = app.state
    table = getattr(state, "mcp_app_looks", None)
    if not isinstance(table, ViewLooks):
        table = ViewLooks()
        state.mcp_app_looks = table
    return table


def publish_look(app: Any, look: PendingLook) -> None:
    """Ask the surface holding the view for a snapshot of it."""
    from clio_agent.gact.events import Event  # noqa: PLC0415

    bus = getattr(app.state, "bus", None)
    if bus is not None:
        # Transient: a surface that mounts while a look waits gets it again (shown route).
        bus.publish(
            Event(
                type=LOOK_EVENT,
                session_id=look.session_id,
                payload=look.event_payload(),
                transient=True,
            )
        )


# ---- readable context -------------------------------------------------------------


def _compact(value: Any, limit: int = 400) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def view_context_lines(context: Any, limit: int = CONTEXT_CHARS) -> list[str]:
    """What a view reported (``ui/update-model-context`` params), as readable lines.

    The view's own text comes first, as it wrote it; each ``structuredContent`` key
    follows on its own line. The whole is cut at ``limit`` characters, and the cut says
    how much was left out.
    """
    if not isinstance(context, Mapping):
        return []
    lines: list[str] = []
    for block in context.get("content") or ():
        if isinstance(block, Mapping) and block.get("type") == "text":
            text = str(block.get("text") or "").strip()
            if text:
                lines.extend(text.splitlines())
    structured = context.get("structuredContent")
    if isinstance(structured, Mapping):
        lines.extend(f"{key}: {_compact(value)}" for key, value in structured.items())
    out: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        if used + len(line) > limit:
            rest = sum(len(item) for item in lines[index:])
            out.append(f"(cut here: {rest} more characters of the view's report)")
            break
        out.append(line)
        used += len(line) + 1
    return out


def coverage_lines(snapshot: Mapping[str, Any]) -> list[str]:
    """What the snapshot painted and what it could not, in words."""
    painted = snapshot.get("painted") if isinstance(snapshot.get("painted"), Mapping) else {}
    drawn = [
        f"{count} {name}{'' if count == 1 else 's'}"
        for name, count in (painted or {}).items()
        if isinstance(count, int) and count > 0 and name != "text"
    ]
    lines = []
    if drawn:
        lines.append("Painted from the view itself: " + ", ".join(drawn) + ".")
    lines.append(
        "The view's HTML (panels, labels, buttons) is drawn approximately: backgrounds and "
        "one-line text, not its exact styling."
    )
    skipped = listed if isinstance(listed := snapshot.get("skipped"), list) else []
    gaps = sorted(
        {
            f"{_SKIP_WORDS.get(str(row.get('reason')), str(row.get('reason')))} "
            f"({row.get('reason')})"
            for row in skipped
            if isinstance(row, Mapping) and row.get("reason")
        }
    )
    if gaps:
        lines.append("Not in the image: " + "; ".join(gaps) + ".")
    return lines


def decode_snapshot_png(data: str) -> tuple[bytes, int, int]:
    """The PNG bytes of a snapshot's data URL, with its pixel size.

    Raises:
        LookAnswerInvalid: not base64 PNG data, or too large.
    """
    from PIL import Image  # noqa: PLC0415

    if len(data) > MAX_SNAPSHOT_BASE64:
        raise LookAnswerInvalid("the snapshot is too large")
    try:
        raw = base64.b64decode(data.split(",", 1)[-1], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise LookAnswerInvalid("the snapshot is not base64 PNG data") from exc
    if not raw.startswith(b"\x89PNG"):
        raise LookAnswerInvalid("the snapshot is not a PNG")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
    except OSError as exc:
        raise LookAnswerInvalid("the snapshot PNG does not decode") from exc
    return raw, width, height


# ---- the agent-facing tool -------------------------------------------------------------


@dataclass(frozen=True)
class ViewIdentity:
    """A live view as the agent names it."""

    app_instance_id: str
    server: str
    tool: str
    title: str
    resource_uri: str
    shown: bool

    def describe(self) -> str:
        where = "on screen" if self.shown else "not on screen"
        return (
            f"“{self.title}” ({self.server or 'server'} view, opened by {self.tool}, "
            f"id {self.app_instance_id}, {where})"
        )


def _views(app: Any, sid: str) -> list[ViewIdentity]:
    from clio_agent.gact.mcp_app_calls import saved_call  # noqa: PLC0415
    from clio_agent.gact.mcp_apps import _registry  # noqa: PLC0415

    table = look_table(app)
    views = []
    for record in sorted(_registry(app).records_for_session(sid), key=lambda r: r.created_at):
        saved = saved_call(app, sid, record.app_instance_id) or {}
        tool = str(record.tool_name or "")
        views.append(
            ViewIdentity(
                app_instance_id=record.app_instance_id,
                server=str(record.source_namespace or ""),
                tool=tool,
                title=str(saved.get("label") or tool.replace("_", " ").strip() or "view"),
                resource_uri=str(record.resource_uri or ""),
                shown=table.shown(sid, record.app_instance_id),
            )
        )
    return views


def _choose(views: Sequence[ViewIdentity], name: str) -> tuple[ViewIdentity | None, str]:
    """The view ``name`` means; else ``None`` and the typed reason."""
    if not views:
        return None, VIEW_NONE
    wanted = name.strip().lower()
    if not wanted:
        shown = [view for view in views if view.shown]
        if len(views) == 1:
            return views[0], ""
        if len(shown) == 1:
            return shown[0], ""
        return None, VIEW_AMBIGUOUS
    exact = [view for view in views if view.app_instance_id.lower() == wanted]
    if exact:
        return exact[0], ""
    matches = [
        view
        for view in views
        if wanted in view.title.lower()
        or wanted in view.server.lower()
        or wanted in view.tool.lower()
        or wanted in view.resource_uri.lower()
    ]
    shown = [view for view in matches if view.shown]
    if len(matches) == 1:
        return matches[0], ""
    if len(shown) == 1:
        return shown[0], ""
    return None, VIEW_AMBIGUOUS if matches else VIEW_NONE


def _identity_fields(view: ViewIdentity) -> dict[str, Any]:
    return {
        "app_instance_id": view.app_instance_id,
        "server": view.server,
        "tool": view.tool,
        "title": view.title,
        "resource_uri": view.resource_uri,
    }


def _refusal(reason: str, text: str, **extra: Any) -> dict[str, Any]:
    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        declare_structured_content,
    )

    trace.event("APPS", "%s %s", reason, " ".join(f"{k}={v}" for k, v in extra.items()))
    row = {"ok": False, "reason": reason, "text": text, **extra}
    declare_structured_content(row)
    return row


def _watched(app: Any, sid: str) -> bool:
    """Whether a window shows this conversation now (one is subscribed to its events)."""
    bus = getattr(app.state, "bus", None)
    count = getattr(bus, "subscriber_count", None)
    return bool(count(sid)) if callable(count) else True


def _not_shown_text(view: ViewIdentity) -> str:
    return (
        f"The view {view.describe()} is not on screen, so it cannot be looked at. The person "
        f"can show it by opening its card in the conversation or its tab; calling {view.tool} "
        "again opens a new view."
    )


def _context_for(app: Any, sid: str, view: ViewIdentity) -> tuple[list[str], str]:
    from clio_agent.gact.mcp_app_context import stored_app_context  # noqa: PLC0415

    entry = stored_app_context(app, sid, view.app_instance_id) or {}
    return view_context_lines(entry.get("context")), str(entry.get("sha256") or "")


def _model_text(view: ViewIdentity, context: list[str], snapshot: Mapping[str, Any]) -> str:
    lines = [
        f"This image is what the view {view.describe()} shows now, as the person sees it "
        f"({snapshot.get('width')}x{snapshot.get('height')} CSS pixels on screen)."
    ]
    if context:
        lines.append("What the view last reported:")
        lines.extend(f"  {line}" for line in context)
    else:
        lines.append("The view has reported no context.")
    lines.extend(coverage_lines(snapshot))
    return "\n".join(lines)


def _save(app: Any, sid: str, png: bytes, call_id: str) -> dict[str, Any]:
    """Keep the image: a session snapshot the history reads, and an artifact the person
    can open from the transcript."""
    from clio_agent import paths  # noqa: PLC0415
    from clio_agent.gact import viewed_media  # noqa: PLC0415
    from clio_agent.gact.artifacts.minting import (  # noqa: PLC0415
        _session_workspace_id,
        _workspace_root,
        artifact_name_for_path,
        mint_artifact,
    )
    from clio_agent.gact.artifacts.records import ArtifactKind, Mechanism  # noqa: PLC0415
    from clio_agent.gact.artifacts.storage import ingest_artifact_identity  # noqa: PLC0415

    workspace_id = _session_workspace_id(app, sid)
    root = _workspace_root(app, workspace_id)
    if root is None:
        raise LookAnswerInvalid("this session has no resolved workspace")
    folder = (paths.workspace_state_dir(root) / "app-view-looks").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{call_id}.png"
    target.write_bytes(png)
    # Kept where the history reads it back: the call's own tool workspace (as view_image).
    snapshot, snapshot_hash = viewed_media.snapshot(png, ".png")
    row: dict[str, Any] = {
        "path": str(target),
        "snapshot": snapshot,
        "snapshot_sha256": snapshot_hash,
    }
    ingested = ingest_artifact_identity(app, target, workspace_root=root)
    version = mint_artifact(
        app,
        sid,
        name=artifact_name_for_path(target),
        workspace_id=workspace_id,
        evidence=ingested.evidence,
        kind=ArtifactKind.IMAGE,
        mechanism=Mechanism.HARNESS,
        producer={"designation": "app_view_look", "purpose": "verification", "session_id": sid},
        custody=ingested.custody,
        path=str(target),
        ingested=ingested,
        not_ingested_size=ingested.not_ingested_size,
    )
    if version is not None:
        row["artifact_id"] = version.artifact_id
    return row


def look_at_view(view: str = "") -> dict[str, Any]:
    """Look at an app view that is open in this conversation, as the person sees it.

    Returns an image of what the view shows right now, with the context it last
    reported (its time step, camera, selection and the like) and its identity. Use it
    to see a 3D scene, a structure, a plot or any other app view before you describe
    it or act on it. ``view`` names the view by its title, server, tool or id; leave it
    empty when only one view is open. A view that is not on screen cannot be looked
    at; the answer says how to show it.
    """
    from clio_agent.gact.agents.tool_instrumentation import (  # noqa: PLC0415
        declare_structured_content,
    )
    from clio_agent.gact.mcp_app_tools import _cancelled, _session  # noqa: PLC0415
    from clio_agent.gact.model_media import deliver  # noqa: PLC0415
    from clio_agent.gact.view_image_tool import VIEW_IMAGE_DESCRIPTOR_TYPE  # noqa: PLC0415

    app, sid = _session()
    if app is None or not sid:
        raise RuntimeError("look_at_view can only run inside a session")
    views = _views(app, sid)
    chosen, reason = _choose(views, view)
    if chosen is None:
        listing = "; ".join(item.describe() for item in views)
        text = (
            "No app view is open in this conversation."
            if reason == VIEW_NONE and not views
            else f"No open view matches “{view}”. Open views: {listing}."
            if reason == VIEW_NONE
            else f"Several views are open; name one. Open views: {listing}."
        )
        return _refusal(reason, text, views=[_identity_fields(item) for item in views])
    table = look_table(app)
    if not chosen.shown or not _watched(app, sid):
        return _refusal(VIEW_NOT_SHOWN, _not_shown_text(chosen), view=_identity_fields(chosen))
    look = table.begin(sid, chosen.app_instance_id)
    publish_look(app, look)
    unwatched = False
    try:
        # No timeout (rule 6): the look ends when the surface answers, the app closes, the
        # turn is cancelled, or no window shows the conversation any more (a browser closed
        # without unloading its frames cannot answer).
        while not look.done.wait(0.25):
            if _cancelled(app):
                from clio_agent.gact.agents.clio_react import _turn_cancelled  # noqa: PLC0415

                raise _turn_cancelled()
            if not _watched(app, sid):
                unwatched = True
                break
    finally:
        table.forget(look)
    identity = _identity_fields(chosen)
    if unwatched:
        table.set_shown(sid, chosen.app_instance_id, "", False)
        return _refusal(VIEW_NOT_SHOWN, _not_shown_text(chosen), view=identity)
    if look.closed:
        return _refusal(VIEW_CLOSED, _not_shown_text(chosen), view=identity)
    answer = look.answer or {}
    error = answer.get("error")
    if isinstance(error, Mapping) or not answer.get("image"):
        error = error if isinstance(error, Mapping) else {}
        reason = str(error.get("reason") or "snapshot_failed")
        message = str(error.get("message") or "The view did not return a snapshot.")
        return _refusal(
            reason,
            f"The view {chosen.describe()} could not be looked at: {message} ({reason}).",
            view=identity,
        )
    png, width_px, height_px = decode_snapshot_png(str(answer["image"]))
    context, context_sha = _context_for(app, sid, chosen)
    saved = _save(app, sid, png, look.call_id)
    model_text = _model_text(chosen, context, answer)
    descriptor = {
        "type": VIEW_IMAGE_DESCRIPTOR_TYPE,
        "path": saved["path"],
        "media_type": "image/png",
        "size_bytes": len(png),
        "sha256": hashlib.sha256(png).hexdigest(),
        "snapshot": saved["snapshot"],
        "snapshot_sha256": saved["snapshot_sha256"],
        **({"artifact_id": saved["artifact_id"]} if "artifact_id" in saved else {}),
        "view": identity,
        "pixels": [width_px, height_px],
        "context_lines": context,
        "context_sha256": context_sha,
        "coverage": {
            "painted": answer.get("painted") or {},
            "skipped": answer.get("skipped") or [],
        },
        "model_text": model_text,
    }
    trace.event(
        "APPS",
        "mcp_app_look app=%s pixels=%sx%s skipped=%d",
        chosen.app_instance_id,
        width_px,
        height_px,
        len(descriptor["coverage"]["skipped"]),
    )
    delivered = deliver(descriptor, tool="look_at_view")
    declare_structured_content(delivered)
    return delivered


def build_look_at_view_tool() -> Any:
    """Build the declared native tool."""
    from clio_agent.gact.agents.tool_instrumentation import native_tool  # noqa: PLC0415
    from clio_agent.gact.mcp_app_look_presentation import present  # noqa: PLC0415

    return native_tool(
        look_at_view,
        name="look_at_view",
        presentation=present,
        domain="surfaces",
        desc=look_at_view.__doc__,
        title="Look at view",
        args={
            "view": {
                "type": "string",
                "description": (
                    "The view's title, server, tool or id; empty when one view is open."
                ),
            }
        },
        read_only=True,
    )
