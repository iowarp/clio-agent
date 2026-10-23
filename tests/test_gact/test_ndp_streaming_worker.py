"""Production Kafka adapter retains startup batches and explicit cursor positions."""

from __future__ import annotations

import hashlib
import io
import json
import sys
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from clio_agent.gact.storage import ndp_streaming_worker as worker
from clio_agent.gact.storage.ndp_streaming_worker import PositionedConsumer, catalog_topic

Partition = namedtuple("Partition", "topic partition")
Message = namedtuple("Message", "topic partition offset timestamp value")


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """A test-only Kafka peer with retained offsets and real encoded payloads."""
    peers = []

    class Peer:
        def __init__(self, **config: Any) -> None:
            self.position, self.closed, self.config = 0, False, config
            self.earliest, self.latest = 0, 575
            peers.append(self)

        def partitions_for_topic(self, topic: str) -> set[int]:
            return {0}

        def beginning_offsets(self, partitions: list[Any]) -> dict[Any, int]:
            return dict.fromkeys(partitions, self.earliest)

        def end_offsets(self, partitions: list[Any]) -> dict[Any, int]:
            return dict.fromkeys(partitions, self.latest)

        def assign(self, partitions: list[Any]) -> None:
            self.partitions = partitions

        def seek(self, partition: Any, offset: int) -> None:
            self.position = offset

        def poll(self, **kwargs: Any) -> dict[Any, list[Any]]:
            rows = [
                Message("science", 0, offset, 123, json.dumps({"measured": offset}).encode())
                for offset in range(self.position, min(self.latest, self.position + 500))
            ]
            self.position += len(rows)
            if callback := getattr(self, "during_poll", None):
                callback()
            return {self.partitions[0]: rows} if rows else {}

        def close(self) -> None:
            self.closed = True

    module = Mock(KafkaConsumer=Peer, TopicPartition=Partition)
    monkeypatch.setitem(sys.modules, "kafka", module)
    return peers


def test_startup_poll_retains_all_575_messages(broker: list[Any]) -> None:
    """The first 500 messages reach the SDK loop instead of disappearing during startup."""
    adapter = PositionedConsumer("science", {"0": 0}, 575, enable_auto_commit=False)
    assert adapter.poll(timeout_ms=0) == {}
    assert broker[0].position == 500
    adapter.release_startup()
    batches = [adapter.poll(timeout_ms=0), adapter.poll(timeout_ms=0)]
    rows = [
        json.loads(message.value)
        for batch in batches
        for messages in batch.values()
        for message in messages
    ]
    assert [row["offset"] for row in rows] == list(range(575))
    assert rows[0]["value"] == {"measured": 0}
    assert (
        rows[0]["source_sha256"] == hashlib.sha256(json.dumps({"measured": 0}).encode()).hexdigest()
    )
    assert adapter.poll(timeout_ms=0) == {}
    assert broker[0].config["group_id"] is None


def test_sdk_startup_seeks_do_not_reset_saved_positions(broker: list[Any]) -> None:
    """A finite capture can stop mid-batch and resume from its last retained offset."""
    adapter = PositionedConsumer("science", {"0": 100}, 10)
    adapter.seek_to_beginning()
    adapter.seek_to_end()
    adapter.poll(timeout_ms=0)
    adapter.release_startup()
    rows = [
        json.loads(message.value)
        for messages in adapter.poll(timeout_ms=0).values()
        for message in messages
    ]
    assert [row["offset"] for row in rows] == list(range(100, 110))
    assert adapter.poll(timeout_ms=0) == {}


def test_release_during_a_poll_keeps_older_startup_batch_first(broker: list[Any]) -> None:
    """Releasing the background reader cannot reorder a concurrently returned batch."""
    adapter = PositionedConsumer("science", {"0": 0}, 575)
    assert adapter.poll(timeout_ms=0) == {}
    broker[0].during_poll = adapter.release_startup
    assert adapter.poll(timeout_ms=0) == {}
    batches = [adapter.poll(timeout_ms=0), adapter.poll(timeout_ms=0)]
    offsets = [
        json.loads(message.value)["offset"]
        for batch in batches
        for messages in batch.values()
        for message in messages
    ]
    assert offsets == list(range(575))


@pytest.mark.parametrize(
    "positions, message", [({"0": 600}, "offsets reset"), ({"1": 0}, "partition history changed")]
)
def test_changed_history_fails_and_closes_consumer(
    broker: list[Any], positions: dict[str, int], message: str
) -> None:
    """Invalid positions never silently restart at the beginning or the live end."""
    with pytest.raises(ValueError, match=message):
        PositionedConsumer("science", positions, 10)
    assert broker[0].closed


def test_catalog_source_cannot_redirect_private_broker_credentials() -> None:
    """Catalog metadata must resolve to the operator-approved broker or explicit alias."""
    ep = Mock(base_url="https://ndp.example")
    ep.session.get.return_value.json.return_value = {
        "format": "kafka",
        "package_id": "dataset",
        "name": "science",
    }
    ep.search_datasets.return_value = [
        {
            "id": "dataset",
            "extras": [
                {"key": "host", "value": "different.example"},
                {"key": "port", "value": "9092"},
            ],
        }
    ]
    with pytest.raises(PermissionError, match="different Kafka broker"):
        catalog_topic(ep, "resource", {}, "approved.example", 9092)
    assert (
        catalog_topic(
            ep,
            "resource",
            {"source_aliases": [{"host": "different.example", "port": 9092}]},
            "approved.example",
            9092,
        )
        == "science"
    )
    ep.session.get.assert_called_with(
        "https://ndp.example/resource/resource", params={"server": "local"}
    )


def test_catalog_source_requires_accessible_parent_and_kafka_resource() -> None:
    """A guessed raw topic or inaccessible dataset cannot bypass catalog selection."""
    ep = Mock(base_url="https://ndp.example")
    ep.session.get.return_value.json.return_value = {"format": "CSV"}
    with pytest.raises(ValueError, match="Choose a Kafka resource"):
        catalog_topic(ep, "resource", {}, "broker", 9092)
    ep.session.get.return_value.json.return_value = {"format": "kafka", "package_id": "private"}
    ep.search_datasets.return_value = []
    with pytest.raises(ValueError, match="parent dataset is not accessible"):
        catalog_topic(ep, "resource", {}, "broker", 9092)


@pytest.mark.parametrize("kind", ["runtime", "permission", "kafka"])
def test_worker_reports_expected_failures_without_exposing_credentials(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The subprocess protocol keeps typed SDK failures useful and redacts all secrets."""

    class KafkaFailure(Exception):
        pass

    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps({"kafka": {"username": "private-user", "password": "private-password"}})
    )
    error_type = {"runtime": RuntimeError, "permission": PermissionError, "kafka": KafkaFailure}[
        kind
    ]

    def fail(request: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
        raise error_type("private-token private-user private-password unavailable")

    monkeypatch.setattr(
        worker.importlib, "import_module", lambda name: SimpleNamespace(KafkaError=KafkaFailure)
    )
    monkeypatch.setattr(worker, "perform", fail)
    monkeypatch.setattr(worker.threading, "Timer", Mock(return_value=Mock()))
    monkeypatch.setattr(worker.logging, "disable", Mock())
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {"profile_path": str(profile), "runtime_seconds": 10, "token": "private-token"}
            )
        ),
    )
    worker.main()
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["error"] == error_type.__name__
    assert result["message"] == "[REDACTED] [REDACTED] [REDACTED] unavailable"
    assert not output.err
