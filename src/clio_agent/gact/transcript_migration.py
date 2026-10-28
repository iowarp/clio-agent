"""One-time, data-only custody of legacy JSON; normal transcript reads never import it."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from functools import partial
from typing import Any

from clio_agent.gact.types import Message

LEGACY_SCOPE = "_transcript_legacy_import/"
#: Done marker of the one complete pass. Conversations created after it are Core-native
#: (their transcript was never legacy JSON), so later boots skip the whole scan.
IMPORT_DONE = "transcript_legacy_import"


def migrate_transcripts(app: Any) -> None:
    """Retain original source and occurrence order in Core before cache writes resume.

    A divergent cache cannot establish transcript visibility: it may predate
    streamed output or survive a clear/delete. Such variants remain inspectable
    evidence with unknown visibility and delivery. Migration never changes the
    canonical transcript, acknowledges mail or starts execution.
    """
    from clio_agent.gact.transcript_projection import assemble_session_messages

    cache = getattr(app.state, "message_store", None)
    if cache is None:
        return
    history = app.state.task_history
    if history.index.done_marker(IMPORT_DONE) is not None:
        return
    store = history.arc._segments
    sessions = set(app.state.sessions.ids()) | set(cache.session_ids())
    for sid in sorted(sessions):
        with history.serialized(writing=True), store._lock_for(sid, LEGACY_SCOPE):
            if store.list_segments(sid, LEGACY_SCOPE, include_tombstoned=False):
                continue  # A completed import also prevents stale cache resurrection later.
            source = cache.read_source(sid)
            receipt: dict[str, Any] = {"version": 1, "source": None, "occurrences": []}
            if source is not None:
                digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
                reference = history.write_content(
                    sid, {"source": f"messages/{sid}.json", "sha256": digest, "text": source}
                )
                receipt.update(source=reference.model_dump(mode="json"), source_sha256=digest)
                try:
                    raw = json.loads(source)
                    if not isinstance(raw, list):
                        raise ValueError("Legacy transcript is not a message list")
                    messages = [Message.model_validate(row) for row in raw]
                except ValueError as error:
                    receipt["parse_error"] = str(error)
                else:
                    canonical = Counter(
                        row.model_dump_json() for row in assemble_session_messages(history.arc, sid)
                    )
                    for index, message in enumerate(messages):
                        encoded = message.model_dump_json()
                        matched = message.session_id == sid and canonical[encoded] > 0
                        if matched:
                            canonical[encoded] -= 1
                        receipt["occurrences"].append(
                            {
                                "identity": hashlib.sha256(
                                    f"{sid}:{digest}:{index}".encode()
                                ).hexdigest(),
                                "index": index,
                                "message_id": message.id,
                                "visibility": "canonical_match" if matched else "unknown",
                                "delivery": "unknown",
                            }
                        )
            history.confirm_write(partial(store.append, sid, LEGACY_SCOPE, "observation", receipt))
    completed = {
        "version": 1,
        "completed_at": datetime.now(UTC).isoformat(),
        "sessions": len(sessions),
    }
    history.confirm_write(partial(history.index.put_done_marker, IMPORT_DONE, completed))


def legacy_transcript(app: Any, session_id: str) -> dict[str, Any] | None:
    """Read preserved evidence without assigning visibility or acknowledging delivery."""
    from clio_agent.arc.task_types import CoreReference

    history = app.state.task_history
    with history.serialized():
        rows = history.arc._segments.list_segments(
            session_id, LEGACY_SCOPE, include_tombstoned=False
        )
        if not rows:
            return None
        receipt = dict(rows[-1].content)
        reference = receipt.get("source")
        receipt["content"] = (
            history.read_content(CoreReference.model_validate(reference)) if reference else None
        )
        return receipt
