"""A hook the OS refuses to start is a typed, recorded failure, never a silently lost event.

The SubprocessAdapter typed only ``FileNotFoundError`` at spawn. Any other ``OSError``
from ``Popen`` (Windows ``ERROR_NOT_ENOUGH_MEMORY``, a paging-file or handle limit under
a loaded full-suite run) escaped the dispatcher, and the SemanticEvent sink then
swallowed it with a bare ``except: pass``. That event's hook simply never ran, with no
trace at all -- one way ``test_semantic_event_hook_fires`` can see every row but one.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from clio_agent.gact.events import EventBus
from clio_agent.gact.hooks import adapters, install_global_dispatcher
from clio_agent.gact.hooks.wire import HookEnvelope, HookInfraError, hook_reasons
from clio_agent.gact.semantic_events import (
    NoopSemanticTraceBackend,
    SemanticEvent,
    SemanticEventSink,
)

from ._hook_fixtures import make_command_dispatcher


def _refuse(*_args: object, **_kwargs: object) -> subprocess.Popen[str]:
    raise OSError(8, "Not enough memory resources are available to process this command")


def test_a_refused_spawn_is_a_typed_hook_infra_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dispatcher = make_command_dispatcher(tmp_path, event="SemanticEvent", body="print('x')")
    [entry] = dispatcher.matching("SemanticEvent", HookEnvelope(hook_event_name="SemanticEvent"))
    monkeypatch.setattr(adapters.subprocess, "Popen", _refuse)
    before = len(hook_reasons())

    with pytest.raises(HookInfraError) as info:
        adapters.SubprocessAdapter().invoke(entry, HookEnvelope(hook_event_name="SemanticEvent"))

    assert info.value.reason == "hook_spawn_failed"
    recorded = hook_reasons()[before:]
    assert [row["reason"] for row in recorded] == ["hook_spawn_failed"]
    assert "Not enough memory" in recorded[0]["error"]


def test_a_semantic_hook_that_cannot_start_is_recorded_and_the_turn_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install_global_dispatcher(
        make_command_dispatcher(tmp_path, event="SemanticEvent", body="print('x')")
    )
    monkeypatch.setattr(adapters.subprocess, "Popen", _refuse)
    sink = SemanticEventSink(bus=EventBus(), trace_backend=NoopSemanticTraceBackend())
    before = len(hook_reasons())
    try:
        with caplog.at_level(logging.WARNING):
            sink.emit(
                SemanticEvent(event_type="turn.started", session_id="s", trace_id="t", summary="x")
            )
    finally:
        install_global_dispatcher(None)

    assert [row["reason"] for row in hook_reasons()[before:]] == ["hook_spawn_failed"]


def test_a_dispatch_failure_outside_the_dispatcher_is_logged_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import clio_agent.gact.hooks as hooks

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("dispatcher exploded")

    monkeypatch.setattr(hooks, "dispatch_semantic_event", _boom)
    sink = SemanticEventSink(bus=EventBus(), trace_backend=NoopSemanticTraceBackend())
    with caplog.at_level(logging.WARNING, logger="clio_agent.gact.semantic_events"):
        sink.emit(
            SemanticEvent(event_type="turn.started", session_id="s", trace_id="t", summary="x")
        )

    assert any(
        "semantic_hook_dispatch_failed event=turn.started" in record.getMessage()
        and "dispatcher exploded" in record.getMessage()
        for record in caplog.records
    )
