"""Runtime boundary regressions use real source/install files and a real turn runner."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clio_agent.gact.agent_blueprints import (
    install_agent_blueprint,
    update_installed_agent_blueprint,
)
from clio_agent.gact.blueprint_reload import apply_blueprint_change
from clio_agent.gact.turn_runner import TurnRunner


def _app() -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            agent=None,
            turn_runner=TurnRunner({}),
            sessions=SimpleNamespace(list=lambda: []),
            bus=SimpleNamespace(publish=lambda event: None),
        )
    )


def _install(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    (source / "experts").mkdir(parents=True)
    (source / "AGENT.md").write_text(
        "---\nid: demo\nversion: 1.0\ntitle: Demo\nroot_expert: main\n---\nOriginal\n"
    )
    (source / "experts/main.md").write_text(
        "---\nid: main\ntitle: Main\ntier: 1\nmodule:\n  kind: react\nprompt_id: demo.main\n---\nCoordinate.\n"
    )
    result = install_agent_blueprint(source=str(source), scope="workspace", cwd=tmp_path)
    return source, Path(result["installed"][0]["root"])


@pytest.mark.asyncio
async def test_reload_retains_whole_old_turn_then_applies_for_queued_turn(tmp_path: Path) -> None:
    source, root = _install(tmp_path)
    app = _app()
    runner = app.state.turn_runner
    entered, finish = asyncio.Event(), asyncio.Event()
    before = (root / "AGENT.md").read_text()
    (source / "AGENT.md").write_text(before.replace("Original", "New revision"))
    seen: list[str] = []

    async def turn() -> None:
        entered.set()
        await finish.wait()
        seen.append((root / "AGENT.md").read_text())

    first = runner.spawn(turn(), sid="first", turn_id="old")
    await entered.wait()
    reload = asyncio.create_task(
        apply_blueprint_change(
            app,
            lambda: update_installed_agent_blueprint(
                blueprint_id="demo",
                scope="workspace",
                cwd=tmp_path,
            ),
            label="Reload test",
        )
    )
    # Observe the gate actually closed, rather than assuming an arbitrary delay.
    for _ in range(100):
        if not runner.revision_gate._open.is_set():
            break
        await asyncio.sleep(0.01)
    assert not runner.revision_gate._open.is_set()
    later = runner.spawn(turn(), sid="second", turn_id="new")
    assert (root / "AGENT.md").read_text() == before
    finish.set()
    result = await asyncio.wait_for(reload, timeout=10)
    await asyncio.gather(first, later)
    assert "Original" in seen[0]
    assert "New revision" in seen[1]
    assert result["operation"]["status"] == "applied"
    assert result["installed"][0]["install"]["checksum"]


@pytest.mark.asyncio
async def test_client_cancellation_does_not_release_boundary_during_copy() -> None:
    app = _app()
    entered, finish = threading.Event(), threading.Event()
    seen: list[str] = []

    def change() -> dict[str, Any]:
        entered.set()
        assert finish.wait(5)
        seen.append("committed")
        return {"installed": []}

    async def later() -> None:
        seen.append("next turn")

    request = asyncio.create_task(apply_blueprint_change(app, change, label="Cancel test"))
    assert await asyncio.to_thread(entered.wait, 5)
    request.cancel()
    queued = app.state.turn_runner.spawn(later(), sid="next", turn_id="next")
    await asyncio.sleep(0)
    assert seen == []
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await request
    await queued
    assert seen == ["committed", "next turn"]
