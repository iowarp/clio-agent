"""A write clio-core does not accept fails typed; memory never holds what clio-core lacks.

The segment store changed its in-memory scope first and persisted after. A failed put
dropped the "offending" segment from memory (SEGMENT-DROP) or left it there while
clio-core never stored it, so the agent could see context clio-core does not have. Now a
failed persist discards the scope's in-memory copy (the next read reloads clio-core's
record) and raises :class:`ArcPersistError`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent.arc.batch_put import BatchPutError
from clio_agent.arc.memory import ARCMemory
from clio_agent.arc.segments import ArcPersistError


def _texts(arc: ARCMemory) -> list[str]:
    return [s.content["text"] for s in arc.render_working_set("s1", "agentA")]


def test_a_failed_put_raises_and_memory_matches_clio_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    arc = ARCMemory(data_dir=str(tmp_path / "arc"))
    arc.append_segment("s1", "agentA", "thought", {"text": "kept"}, step=0)
    store = arc._store
    real_put_many = store.put_many
    failures = {"left": 1}

    def flaky_put_many(kind: str, records: Any) -> None:
        # The append's batch (lane chunk + search companion): clio-core keeps none of it.
        if kind == "segments" and failures["left"]:
            failures["left"] -= 1
            raise BatchPutError(kind, [], {r.name: RuntimeError("refused") for r in records})
        real_put_many(kind, records)

    monkeypatch.setattr(store, "put_many", flaky_put_many)

    with pytest.raises(ArcPersistError):
        arc.append_segment("s1", "agentA", "thought", {"text": "never stored"}, step=1)

    assert _texts(arc) == ["kept"], "memory holds exactly what clio-core holds"
    assert _texts(ARCMemory(data_dir=str(tmp_path / "arc"))) == ["kept"]

    arc.append_segment("s1", "agentA", "thought", {"text": "after"}, step=2)
    assert _texts(arc) == ["kept", "after"], "the scope is not wedged"
