"""Live SSE collector for the C1-S6 EXPANDED avenue leg (``leg_c2_v2_avenues.py``).

NEW helper module (leg_c/leg_b never needed this): the waits-cancel avenue
(#5) must observe the ``mcp_task.wait`` surfacing event
(``gact/mcp_task_events.py::publish_mcp_task_wait``) WHILE a task-mode call is
in flight. That event is published ``transient=True`` (``gact/events.py``'s
``EventBus._deliver`` records history only for non-transient events, but still
fans a transient event out to every LIVE subscriber queue -- verified by
reading ``EventBus._deliver`` directly, not assumed). So polling
``GET /v1/sessions/{sid}/events`` *after* a call finishes can never see it: a
subscriber must be attached to the SSE stream *during* the call. There is no
existing helper for this in ``_common.py`` (leg B/C never drove a live SSE
subscription -- their evidence came from message metadata + one headless
question-answer poll), hence this new, small, focused module.

Wire shape confirmed by reading ``gact/routes/misc.py::session_events`` and
``gact/runtime/globals.py::_format_sse`` / ``gact/protocol/v3/event.py::
format_sse_v3``: every frame is ``event: <type>\\n[id: <n>\\n]data: <json>\\n\\n``
-- a blank line ends one frame. This module is a minimal, dependency-free
(``requests`` only, already used throughout this package) SSE line parser
run on a background thread, collecting parsed ``{"event": ..., "data": ...}``
rows into a plain list a caller can inspect while the subscription is still
open (list.append/read is GIL-atomic enough for this single-writer,
single-reader polling pattern -- no lock needed for a append-only list under
CPython).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any


class SSECollector:
    """Background SSE subscriber for one ``GET /v1/sessions/{sid}/events`` feed.

    Usage::

        collector = SSECollector(base, sid)
        collector.start()
        ...drive turns / calls while it is open...
        collector.stop()
        waits = collector.events_of_type("mcp_task.wait")
    """

    def __init__(self, base: str, sid: str, *, connect_timeout: float = 15.0) -> None:
        self._url = f"{base}/v1/sessions/{sid}/events"
        self._connect_timeout = connect_timeout
        self.events: list[dict[str, Any]] = []
        self.connect_error: str | None = None
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._response: Any = None
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"sse-{sid[:8]}")

    def start(self, *, wait_connected: bool = True) -> "SSECollector":
        self._thread.start()
        if wait_connected:
            self._connected.wait(timeout=self._connect_timeout)
        return self

    def _run(self) -> None:
        import requests

        try:
            with requests.get(
                self._url,
                stream=True,
                timeout=(self._connect_timeout, None),
                headers={"Accept": "text/event-stream"},
            ) as resp:
                self._response = resp
                resp.raise_for_status()
                self._connected.set()
                event_type: str | None = None
                data_lines: list[str] = []
                for raw_line in resp.iter_lines(decode_unicode=True):
                    if self._stop.is_set():
                        break
                    if raw_line is None:
                        continue
                    line = raw_line
                    if line == "":
                        if data_lines:
                            payload_text = "\n".join(data_lines)
                            try:
                                payload: Any = json.loads(payload_text)
                            except (ValueError, TypeError):
                                payload = payload_text
                            self.events.append({"event": event_type, "data": payload})
                        event_type = None
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue  # SSE comment/keepalive line -- ignore
                    if line.startswith("event:"):
                        event_type = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:") :].strip())
                    # "id:" lines carry no information this collector needs.
        except Exception as exc:  # noqa: BLE001 - surfaced via connect_error, never raised cross-thread
            self.connect_error = f"{type(exc).__name__}: {exc}"
        finally:
            self._connected.set()

    def stop(self, *, grace_s: float = 1.0) -> None:
        """Signal the background reader to stop and close the connection.

        Closing the underlying response is what actually unblocks a reader
        parked in a blocking ``iter_lines()`` read (the ``_stop`` flag alone
        is only checked between already-received lines); this waits a short
        grace period for the thread to notice before returning, never hangs
        the caller if the socket close is itself slow.
        """

        self._stop.set()
        if self._response is not None:
            try:
                self._response.close()
            except Exception:  # noqa: BLE001 - best-effort teardown must never raise
                pass
        self._thread.join(timeout=grace_s)

    def events_of_type(self, event_type: str) -> list[dict[str, Any]]:
        return [row for row in self.events if row.get("event") == event_type]

    def wait_for_event(
        self, event_type: str, *, max_elapsed: float = 30.0, poll_interval: float = 0.25
    ) -> dict[str, Any] | None:
        """Poll ``self.events`` (already being filled by the background thread)
        for the first row of ``event_type``, bounded -- never a bare sleep."""

        deadline = time.monotonic() + max_elapsed
        while time.monotonic() < deadline:
            found = self.events_of_type(event_type)
            if found:
                return found[0]
            time.sleep(poll_interval)
        return None
