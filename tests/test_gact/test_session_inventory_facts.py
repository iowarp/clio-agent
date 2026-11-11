"""Connected sources and uploaded resources ride session_facts, and only when present."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from clio_agent import injections
from clio_agent.gact.agents.composition import _inventory_lines


class _Resources:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows

    def list(self, workspace_id: str) -> list[Any]:
        return list(self.rows) if workspace_id == "ws_1" else []


class _Sources:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.store = SimpleNamespace(clio_id="clio_a")

    def sources(self, workspace_id: str) -> list[Any]:
        return list(self.rows) if workspace_id == "ws_1" else []


def _resource(i: int) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"res_{i}", name=f"plates_{i}.csv", detected_mime="text/csv", claimed_mime="",
        received_size=2048, declared_size=2048, state="ready",
    )  # fmt: skip


def _source(owner: str) -> SimpleNamespace:
    source = SimpleNamespace(
        id="src_1", label="Field notes", provider="google_drive", local_path="/w/notes",
        owner=SimpleNamespace(clio_id=owner),
    )  # fmt: skip
    return SimpleNamespace(source=source, connected=True, linked_access="read_only")


def _app(resources: list[Any], sources: list[Any]) -> SimpleNamespace:
    state = SimpleNamespace(
        sessions={"sess_1": SimpleNamespace(workspace_id="ws_1")},
        resource_store=_Resources(resources),
        connected_storage=_Sources(sources),
    )
    return SimpleNamespace(state=state)


def _fact(piece: str, variables: dict[str, Any]) -> str | None:
    return injections.render(f"session_facts{piece}", variables)


def test_a_workspace_with_a_source_and_resources_names_them() -> None:
    lines = _inventory_lines(
        _app([_resource(i) for i in range(12)], [_source("clio_a"), _source("clio_other")]),
        "sess_1",
        _fact,
    )
    text = "\n".join(line or "" for line in lines)
    assert "Field notes (google_drive, connected, read-only, at /w/notes; id src_1)" in text
    assert text.count("Field notes") == 1  # another CLIO's source is never described
    assert "Uploaded resources in this workspace (12, newest 10 shown)" in text
    assert "plates_9.csv" in text and "plates_10.csv" not in text


def test_an_empty_workspace_adds_nothing() -> None:
    """SABOTAGE: drop the ``if total`` guard and an empty resources line appears."""
    assert _inventory_lines(_app([], []), "sess_1", _fact) == []
    assert _inventory_lines(_app([], []), "", _fact) == []
