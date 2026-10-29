"""Session projections remain readable while task/mail metadata is updated."""

from __future__ import annotations

from threading import Event, Thread
from typing import Any

from clio_agent.gact.sessions import SessionStore


class _CopyGate:
    """Hold a real dataclass projection while another thread patches the store."""

    def __init__(self, copying: Event, patched: Event) -> None:
        self.copying = copying
        self.patched = patched

    def __deepcopy__(self, memo: dict[int, Any]) -> str:
        if self.copying.is_set():
            return "captured"
        self.copying.set()
        assert self.patched.wait(10), "metadata writer did not finish while projection was held"
        return "captured"


def test_session_projection_keeps_metadata_snapshot_during_store_update() -> None:
    """A concurrent durable metadata patch cannot resize the projection's mapping."""
    store = SessionStore()
    copying, patched = Event(), Event()
    session = store.create(workspace_id="ws", metadata={"first": "captured"})
    # Creation now snapshots records too. Gate only the projection under test;
    # the concurrent writer must be free to serialize its own replacement row.
    session.metadata["first"] = _CopyGate(copying, patched)
    original = session.metadata

    def update_metadata() -> None:
        assert copying.wait(10), "projection did not reach its metadata"
        store.update(session.id, metadata_patch={"agent_message": {"status": "pending"}})
        patched.set()

    writer = Thread(target=update_metadata)
    writer.start()
    try:
        wire = session.to_wire()
    finally:
        writer.join(timeout=10)
    assert not writer.is_alive()
    assert wire["metadata"] == {"first": "captured"}
    assert session.metadata is not original
    assert session.metadata["agent_message"] == {"status": "pending"}
    assert "agent_message" not in original
