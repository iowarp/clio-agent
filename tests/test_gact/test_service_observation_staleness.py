"""F032: a stored observation never outlives the deployment it described."""

from __future__ import annotations

from pathlib import Path

import pytest

from clio_agent.gact.infrastructure.models import ServiceActionRequest, ServiceObservation
from clio_agent.gact.infrastructure.service_settlement import settle_service
from tests.test_gact.test_managed_service_cleanup import (
    FakeLinuxTarget,
    _finish,
    _install,
    _runtime,
)

NATIVE = ServiceObservation(
    definition_version="ollama-native-1",
    phase="stopped",
    installed=True,
    evidence_directory="/old/native/root",
)


@pytest.mark.asyncio
async def test_a_container_status_clears_an_earlier_native_observation(tmp_path: Path) -> None:
    runtime, store, target_id = _runtime(tmp_path, FakeLinuxTarget())
    installed = await _finish(runtime, store, "ollama", _install(target_id))
    assert installed.state == "succeeded", installed.error
    store.update_service(target_id, "ollama", observation=NATIVE)

    catalog = await runtime.catalog(target_id)

    service = next(row for row in catalog.services if row.id == "ollama")
    assert service.state == "running"
    assert service.observation is None
    record = store.service(target_id, "ollama")
    assert record is not None and record.observation is None


@pytest.mark.asyncio
async def test_a_variant_switch_does_not_inherit_the_previous_observation(
    tmp_path: Path,
) -> None:
    runtime, store, target_id = _runtime(tmp_path, FakeLinuxTarget())
    installed = await _finish(runtime, store, "ollama", _install(target_id))
    assert installed.state == "succeeded", installed.error
    store.update_service(target_id, "ollama", observation=NATIVE)

    same = _install(target_id)
    await settle_service(runtime, "ollama", same.model_copy(update={"action": "stop"}), None, [])
    record = store.service(target_id, "ollama")
    assert record is not None and record.observation == NATIVE

    switched = ServiceActionRequest(
        target_id=target_id, action="install", variant_id="cuda", configuration={}
    )
    await settle_service(runtime, "ollama", switched, None, [])
    record = store.service(target_id, "ollama")
    assert record is not None and record.variant_id == "cuda"
    assert record.observation is None
