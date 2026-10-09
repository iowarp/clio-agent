"""F047: a provenance provider whose services are down must not abort serve."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from clio_agent.gact.provenance import factory
from clio_agent.gact.provenance.deferred import DeferredProvider, ProviderUnavailableError
from clio_agent.gact.provenance.dispatcher import ProvenanceDispatcher
from clio_agent.gact.provenance.protocol import ExecutionProvenanceReader, ProviderReceipt


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _Inner:
    name = "flowcept"
    durable = False
    queryable = True

    def __init__(self) -> None:
        self.events: list[Any] = []
        self.closed = False

    def emit(self, event: Any) -> ProviderReceipt:
        self.events.append(event)
        return ProviderReceipt.ACCEPTED

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def query_tasks(self, filter: dict[str, Any], **_: Any) -> list[dict[str, Any]]:
        return [{"filter": filter}]

    def query_workflows(self, filter: dict[str, Any]) -> list[dict[str, Any]]:
        return [filter]

    def query_execution(self, **kwargs: Any) -> dict[str, Any]:
        return {"session_id": kwargs["session_id"]}


class _FlakyBuild:
    """Fails until ``up`` is set, like Flowcept before its Redis starts."""

    def __init__(self) -> None:
        self.up = False
        self.calls = 0
        self.inner = _Inner()

    def __call__(self) -> _Inner:
        self.calls += 1
        if not self.up:
            raise ConnectionError("Error 111 connecting to 127.0.0.1:36379. Connection refused.")
        return self.inner


def _deferred(build: _FlakyBuild, clock: _Clock) -> DeferredProvider:
    return DeferredProvider(
        "flowcept", build, durable=False, queryable=True, retry_seconds=30.0, clock=clock
    )


def test_construction_failure_degrades_instead_of_raising() -> None:
    build, clock = _FlakyBuild(), _Clock()
    provider = _deferred(build, clock)
    assert not provider.attached
    assert "ConnectionError" in provider.unavailable_reason
    with pytest.raises(ProviderUnavailableError, match="provider_unavailable: flowcept"):
        provider.emit(object())
    assert provider.query_tasks({"x": 1}) is None
    assert provider.query_workflows({}) is None
    with pytest.raises(ProviderUnavailableError):
        provider.query_execution(session_id="s", child_session_ids=[], limit=1)


def test_retry_is_bounded_by_interval_then_attaches() -> None:
    build, clock = _FlakyBuild(), _Clock()
    provider = _deferred(build, clock)
    assert build.calls == 1
    provider.query_tasks({})
    assert build.calls == 1  # inside the retry window: no reconnect storm
    build.up = True
    clock.now = 31.0
    assert provider.query_tasks({"a": 1}) == [{"filter": {"a": 1}}]
    assert provider.attached and provider.unavailable_reason == ""
    assert build.calls == 2
    assert provider.emit("ev") == ProviderReceipt.ACCEPTED
    assert build.inner.events == ["ev"]
    provider.close()
    assert build.inner.closed


def test_healthy_backend_attaches_at_startup() -> None:
    build, clock = _FlakyBuild(), _Clock()
    build.up = True
    provider = _deferred(build, clock)
    assert provider.attached and build.calls == 1


def test_closed_provider_never_attaches() -> None:
    build, clock = _FlakyBuild(), _Clock()
    provider = _deferred(build, clock)
    provider.close()
    build.up = True
    clock.now = 100.0
    assert provider.query_tasks({}) is None
    assert build.calls == 1


def test_dispatcher_health_unavailable_then_ready() -> None:
    build, clock = _FlakyBuild(), _Clock()
    provider = _deferred(build, clock)
    dispatcher = ProvenanceDispatcher([provider], queue_size=8)
    try:
        row = dispatcher.health()[0]
        assert row["status"] == "unavailable"
        assert str(row["last_error"]).startswith("provider_unavailable: ConnectionError")
        dispatcher.emit("dropped")  # type: ignore[arg-type]
        dispatcher.flush()
        row = dispatcher.health()[0]
        assert row["status"] == "unavailable" and row["failed"] == 1
        # The reader stays discoverable so routes report a query failure, not
        # "not configured".
        assert isinstance(dispatcher.reader("flowcept"), ExecutionProvenanceReader)

        build.up = True
        clock.now = 31.0
        dispatcher.emit("kept")  # type: ignore[arg-type]
        dispatcher.flush()
        row = dispatcher.health()[0]
        assert row["status"] == "ready" and row["accepted"] == 1
        assert build.inner.events == ["kept"]
    finally:
        dispatcher.close()


def test_factory_wraps_flowcept_so_serve_survives_down_redis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import clio_agent.gact.provenance.flowcept as flowcept_module

    class _Refusing:
        durable = False
        queryable = True
        flush_durable = False
        flush_note = "no drain"

        def __init__(self, _config: Any) -> None:
            raise ConnectionError("Error 111 connecting to 127.0.0.1:36379. Connection refused.")

    monkeypatch.setattr(factory, "configured_provider_names", lambda: ["jsonl", "flowcept"])
    monkeypatch.setattr(flowcept_module, "FlowceptProvenanceProvider", _Refusing)
    backend = factory.build_provenance_backend(tmp_path)
    try:
        rows = {row["name"]: row for row in backend.health()}
        assert rows["jsonl"]["status"] == "ready"
        assert rows["flowcept"]["status"] == "unavailable"
        assert rows["flowcept"]["flush_durable"] is False
    finally:
        backend.close()


def test_missing_flowcept_settings_file_is_unavailable_not_silently_ready(tmp_path: Path) -> None:
    from clio_agent.gact.provenance.flowcept import (
        FlowceptProvenanceProvider,
        FlowceptProviderConfig,
    )

    missing = tmp_path / "other-host" / "flowcept" / "settings.yaml"
    provider = DeferredProvider(
        "flowcept",
        lambda: FlowceptProvenanceProvider(FlowceptProviderConfig(settings_path=str(missing))),
        durable=False,
        queryable=True,
    )
    assert not provider.attached
    assert "settings file not found" in provider.unavailable_reason
    assert str(missing) in provider.unavailable_reason
