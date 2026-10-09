"""Bounded, revision-matched visual feedback from a mounted A2UI viewer.

The producer thread waits; the HTTP event loop never does. A request belongs
to exactly one viewer and one view epoch. No viewer is silently substituted.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import time
import uuid
from dataclasses import dataclass
from threading import Condition
from typing import Any

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field

MAX_IMAGE_BYTES = 5_000_000
VIEWER_TTL = 4.0


class VisualFeedbackError(ValueError):
    """An explicit unavailable, stale, failed, or timed-out visual operation."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason


class ViewerReport(BaseModel):
    """The mounted renderer's bounded state; definition and view epochs differ."""

    model_config = ConfigDict(extra="forbid", strict=True)
    viewer_id: str = Field(min_length=1, max_length=128)
    surface_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1)
    view_revision: int = Field(ge=0)
    visible: bool
    ready: bool
    state: dict[str, Any] = Field(default_factory=dict)
    artifact_id: str = Field(default="", max_length=128)


class CaptureReply(BaseModel):
    """One claimed viewer's image or explicit failure, never a previous frame."""

    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=128)
    viewer_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    view_revision: int = Field(ge=0)
    png_base64: str = Field(default="", max_length=6_666_668)
    error: str = Field(default="", max_length=2000)
    previous_view_revision: int | None = Field(default=None, ge=0)


@dataclass
class _Viewer:
    report: ViewerReport
    seen: float


@dataclass
class _Capture:
    session_id: str
    report: ViewerReport
    component_id: str
    deadline: float
    claimed: bool = False
    reply: CaptureReply | None = None
    data_model_update: dict[str, Any] | None = None


class A2UIVisualFeedback:
    """Short-lived viewer leases and bounded, single-owner capture requests."""

    def __init__(self) -> None:
        self._condition = Condition()
        self._viewers: dict[tuple[str, str], _Viewer] = {}
        self._requests: dict[str, _Capture] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        self._viewers = {
            key: row for key, row in self._viewers.items() if now - row.seen < VIEWER_TTL
        }

    def report(self, sid: str, report: ViewerReport) -> list[dict[str, Any]]:
        """Renew a viewer lease and atomically claim its assigned requests."""
        if len(json.dumps(report.state)) > 64_000:
            raise VisualFeedbackError("a2ui_view_state_too_large", "Viewer state exceeds 64 KB.")
        with self._condition:
            self._prune()
            key = (sid, report.viewer_id)
            previous = self._viewers.get(key)
            if previous and report.view_revision < previous.report.view_revision:
                raise VisualFeedbackError("a2ui_view_stale", "Viewer epoch moved backwards.")
            if (
                previous
                and report.view_revision == previous.report.view_revision
                and (
                    report.state != previous.report.state
                    or report.revision != previous.report.revision
                    or report.surface_id != previous.report.surface_id
                    or report.artifact_id != previous.report.artifact_id
                )
            ):
                raise VisualFeedbackError(
                    "a2ui_view_stale", "Changed state requires a new view epoch."
                )
            if key not in self._viewers and len(self._viewers) >= 1024:
                raise VisualFeedbackError("a2ui_viewer_limit", "Too many mounted viewers.")
            self._viewers[key] = _Viewer(report.model_copy(deep=True), time.monotonic())
            requests = []
            for request_id, request in self._requests.items():
                if (
                    request.session_id == sid
                    and request.report.viewer_id == report.viewer_id
                    and not request.claimed
                    and request.deadline > time.monotonic()
                ):
                    request.claimed = True
                    requests.append(
                        {
                            "request_id": request_id,
                            "surface_id": request.report.surface_id,
                            "revision": request.report.revision,
                            "view_revision": request.report.view_revision,
                            "component_id": request.component_id,
                            "artifact_id": request.report.artifact_id,
                            **(
                                {"data_model_update": request.data_model_update}
                                if request.data_model_update is not None
                                else {}
                            ),
                        }
                    )
            self._condition.notify_all()
            return requests

    def viewers(self, sid: str, surface_id: str) -> list[dict[str, Any]]:
        """Inspect fresh viewer-local state without mistaking it for producer state."""
        with self._condition:
            self._prune()
            return [
                row.report.model_dump()
                for (session, _), row in self._viewers.items()
                if session == sid and row.report.surface_id == surface_id
            ]

    def complete(self, sid: str, reply: CaptureReply) -> None:
        """Accept only the assigned viewer, while its exact requested view is current."""
        with self._condition:
            request = self._requests.get(reply.request_id)
            if request is None or request.session_id != sid:
                raise VisualFeedbackError(
                    "a2ui_capture_not_found", "Capture expired or is unknown."
                )
            if not request.claimed or request.reply is not None:
                raise VisualFeedbackError(
                    "a2ui_capture_unclaimed", "Capture is unclaimed or completed."
                )
            if reply.viewer_id != request.report.viewer_id:
                raise VisualFeedbackError(
                    "a2ui_capture_wrong_viewer", "Another viewer owns this request."
                )
            if time.monotonic() >= request.deadline:
                raise VisualFeedbackError(
                    "a2ui_capture_timeout", "Capture arrived after its deadline."
                )
            control = request.data_model_update is not None
            self._assert_current(request, allow_epoch_change=control)
            if control:
                current = self._viewers[(sid, reply.viewer_id)].report
                if (
                    reply.previous_view_revision != request.report.view_revision
                    or reply.revision != request.report.revision
                    or reply.view_revision != current.view_revision
                    or (not reply.error and reply.view_revision <= request.report.view_revision)
                ):
                    raise VisualFeedbackError(
                        "a2ui_view_stale",
                        "Control acknowledgement does not match its viewer epochs.",
                    )
            elif (reply.revision, reply.view_revision) != (
                request.report.revision,
                request.report.view_revision,
            ):
                raise VisualFeedbackError(
                    "a2ui_view_stale", "Capture does not match the requested view."
                )
            request.reply = reply
            self._condition.notify_all()

    def _assert_current(self, request: _Capture, *, allow_epoch_change: bool = False) -> None:
        row = self._viewers.get((request.session_id, request.report.viewer_id))
        if row is None or time.monotonic() - row.seen >= VIEWER_TTL or not row.report.visible:
            raise VisualFeedbackError(
                "a2ui_viewer_unavailable", "The requested viewer is no longer visible."
            )
        if (
            row.report.surface_id != request.report.surface_id
            or row.report.artifact_id != request.report.artifact_id
            or row.report.revision != request.report.revision
            or (not allow_epoch_change and row.report.view_revision != request.report.view_revision)
        ):
            raise VisualFeedbackError(
                "a2ui_view_stale", "The view changed during capture; inspect and retry."
            )

    def control(
        self,
        sid: str,
        surface_id: str,
        revision: int,
        *,
        artifact_id: str,
        viewer_id: str,
        view_revision: int,
        path: str,
        value: Any,
    ) -> dict[str, Any]:
        """Apply one declared local binding to a pinned saved view, with an epoch guard."""
        if len(json.dumps(value)) > 4096:
            raise VisualFeedbackError(
                "a2ui_view_value_too_large",
                "View controls are limited to 4 KB; revise source data separately.",
            )
        with self._condition:
            self._prune()
            row = self._viewers.get((sid, viewer_id))
            if row is None or not row.report.visible:
                raise VisualFeedbackError(
                    "a2ui_viewer_unavailable", "Open and inspect the saved dashboard first."
                )
            if (
                row.report.surface_id != surface_id
                or row.report.artifact_id != artifact_id
                or row.report.revision != revision
                or row.report.view_revision != view_revision
            ):
                raise VisualFeedbackError(
                    "a2ui_view_stale", "The selected saved view changed; inspect and retry."
                )
            if len(self._requests) >= 128 or any(
                job.session_id == sid and job.report.viewer_id == viewer_id
                for job in self._requests.values()
            ):
                raise VisualFeedbackError(
                    "a2ui_viewer_busy", "This viewer already has an operation in progress."
                )
            request = _Capture(
                sid,
                row.report.model_copy(deep=True),
                "",
                time.monotonic() + 15,
                data_model_update={"path": path, "value": value},
            )
            request_id = uuid.uuid4().hex
            self._requests[request_id] = request
            try:
                while request.reply is None:
                    self._assert_current(request, allow_epoch_change=True)
                    remaining = request.deadline - time.monotonic()
                    if remaining <= 0:
                        raise VisualFeedbackError(
                            "a2ui_view_control_timeout",
                            "The viewer did not acknowledge the control; inspect its state before retrying.",
                        )
                    self._condition.wait(min(remaining, 0.25))
                self._assert_current(request, allow_epoch_change=True)
                reply = request.reply
                if reply.error:
                    current = self._viewers[(sid, viewer_id)].report
                    if current.view_revision != request.report.view_revision:
                        raise VisualFeedbackError("a2ui_view_stale", reply.error)
                    raise VisualFeedbackError("a2ui_view_control_failed", reply.error)
                current = self._viewers[(sid, viewer_id)].report
                if current.view_revision != reply.view_revision:
                    raise VisualFeedbackError(
                        "a2ui_view_stale", "The view changed after the control acknowledgement."
                    )
                return {
                    "controlled": True,
                    "request_id": request_id,
                    "viewer": current.model_dump(),
                }
            finally:
                self._requests.pop(request_id, None)

    def capture(
        self,
        sid: str,
        surface_id: str,
        revision: int,
        *,
        component_id: str = "",
        viewer_id: str = "",
        view_revision: int | None = None,
        artifact_id: str = "",
        timeout: float = 15,
    ) -> tuple[bytes, dict[str, Any]]:
        """Wait for an eligible mounted renderer and its matching fresh PNG."""
        if not 0 < timeout <= 20:
            raise VisualFeedbackError(
                "a2ui_capture_timeout_invalid", "Timeout must be in (0, 20] seconds."
            )
        with self._condition:
            deadline = time.monotonic() + timeout
            self._prune()
            eligible = [
                row
                for (session, _), row in self._viewers.items()
                if session == sid
                and row.report.surface_id == surface_id
                and row.report.artifact_id == artifact_id
                and row.report.visible
                and (not viewer_id or row.report.viewer_id == viewer_id)
            ]
            if not eligible:
                raise VisualFeedbackError(
                    "a2ui_viewer_unavailable", "Open the requested surface or dashboard first."
                )
            matched = [
                row
                for row in eligible
                if row.report.revision == revision
                and (view_revision is None or row.report.view_revision == view_revision)
            ]
            # A producer update may reach the mounted browser on its next SSE/poll.
            # Wait only for an older definition; never substitute a newer revision
            # or a different explicit human view epoch.
            while view_revision is None and (
                (not matched and any(row.report.revision < revision for row in eligible))
                or (matched and not any(row.report.ready for row in matched))
            ):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(min(remaining, 0.25))
                self._prune()
                eligible = [
                    row
                    for (session, _), row in self._viewers.items()
                    if session == sid
                    and row.report.surface_id == surface_id
                    and row.report.artifact_id == artifact_id
                    and row.report.visible
                    and (not viewer_id or row.report.viewer_id == viewer_id)
                ]
                matched = [row for row in eligible if row.report.revision == revision]
            if matched and not any(row.report.ready for row in matched):
                raise VisualFeedbackError(
                    "a2ui_render_not_ready",
                    "Required data or rendering did not become ready within the capture budget; inspect viewer state.",
                )
            matched = [row for row in matched if row.report.ready]
            if not matched:
                raise VisualFeedbackError(
                    "a2ui_view_stale", "No viewer has the expected definition and view revision."
                )
            if len(self._requests) >= 128:
                raise VisualFeedbackError("a2ui_capture_limit", "Too many captures are pending.")
            matched = [
                row
                for row in matched
                if not any(
                    job.session_id == sid and job.report.viewer_id == row.report.viewer_id
                    for job in self._requests.values()
                )
            ]
            if not matched:
                raise VisualFeedbackError(
                    "a2ui_viewer_busy",
                    "Matching viewers are already capturing; retry after they complete.",
                )
            row = sorted(matched, key=lambda item: item.report.viewer_id)[0]
            request = _Capture(sid, row.report.model_copy(deep=True), component_id, deadline)
            request_id = uuid.uuid4().hex
            self._requests[request_id] = request
            try:
                while request.reply is None:
                    self._assert_current(request)
                    remaining = request.deadline - time.monotonic()
                    if remaining <= 0:
                        raise VisualFeedbackError(
                            "a2ui_capture_timeout",
                            "The viewer did not deliver pixels within the capture budget.",
                        )
                    self._condition.wait(min(remaining, 0.5))
                self._assert_current(request)
                reply = request.reply
                if reply.error:
                    raise VisualFeedbackError("a2ui_capture_failed", reply.error)
                data, dimensions = decode_png(reply.png_base64)
                return data, {
                    "request_id": request_id,
                    "viewer": request.report.model_dump(),
                    "component_id": component_id,
                    **dimensions,
                }
            finally:
                self._requests.pop(request_id, None)


def decode_png(encoded: str) -> tuple[bytes, dict[str, int]]:
    """Verify actual PNG pixels and size before accepting an image from a viewer."""
    try:
        data = base64.b64decode(encoded, validate=True)
        if not 0 < len(data) <= MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds the 5 MB capture limit.")
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if image.format != "PNG" or not (0 < width <= 4096 and 0 < height <= 4096):
                raise ValueError("Capture must be a PNG no larger than 4096 × 4096.")
            image.verify()
    except (ValueError, OSError, binascii.Error, Image.DecompressionBombError) as exc:
        raise VisualFeedbackError("a2ui_capture_invalid_image", str(exc)) from exc
    return data, {"width": width, "height": height}
