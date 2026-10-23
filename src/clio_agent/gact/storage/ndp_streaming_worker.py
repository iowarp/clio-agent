"""Isolated streaming_v2 worker; uses only stdlib and the separately installed SDK.

Input arrives over private stdin. The broker profile stays on the runtime host.
The Endpoint requires no custom stream routes or access to this worker.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import quote


def catalog_topic(
    ep: Any, identifier: str, broker: dict[str, Any], discovered_host: str, discovered_port: int
) -> str:
    """Resolve an authorized catalog resource before passing a topic to the SDK."""
    response = ep.session.get(
        ep.base_url + "/resource/" + quote(identifier, safe=""), params={"server": "local"}
    )
    response.raise_for_status()
    resource = response.json()
    if str(resource.get("format", "")).casefold() != "kafka":
        raise ValueError("Choose a Kafka resource returned by this Endpoint's catalog")
    definition: dict[str, Any] = {}
    try:
        parsed = json.loads(resource.get("description", ""))
        if isinstance(parsed, dict):
            definition = parsed
    except (ValueError, TypeError):
        pass
    matches = ep.search_datasets([resource["package_id"]], keys=["id"], server="local")
    parent = next((row for row in matches if row["id"] == resource["package_id"]), None)
    if parent is None:
        raise ValueError("The stream's parent dataset is not accessible through this Endpoint")
    extras = parent.get("extras", {})
    if isinstance(extras, list):
        extras = {row["key"]: row.get("value") for row in extras}
    host = definition.get("host") or resource.get("host") or extras.get("host")
    port = definition.get("port") or resource.get("port") or extras.get("port")
    allowed = {
        (str(discovered_host), int(discovered_port)),
        *((str(row["host"]), int(row["port"])) for row in broker.get("source_aliases", [])),
    }
    if not host or not port or (str(host), int(port)) not in allowed:
        raise PermissionError(
            "The selected catalog source uses a different Kafka broker; configure its approved SDK profile"
        )
    topic = (
        definition.get("topic")
        or resource.get("topic")
        or extras.get("topic")
        or resource.get("name")
    )
    if not isinstance(topic, str) or not topic:
        raise ValueError("The selected catalog resource does not identify its Kafka topic")
    return topic


class PositionedConsumer:
    """Adapt a real Kafka consumer to SDK startup and CLIO's explicit positions.

    SDK initialization polls can return data before its buffer loop starts. Keep
    those batches until that loop can receive them. Every returned value retains
    its Kafka identity beside the unchanged decoded source payload.
    """

    def __init__(
        self, topic: str, positions: dict[str, int], delivery_limit: int, **configuration: Any
    ) -> None:
        kafka = importlib.import_module("kafka")
        KafkaConsumer, TopicPartition = kafka.KafkaConsumer, kafka.TopicPartition

        configuration["group_id"] = None
        self.consumer = KafkaConsumer(**configuration)
        partitions = self.consumer.partitions_for_topic(topic)
        if not partitions:
            self.consumer.close()
            raise ValueError("The selected Kafka topic has no readable partitions")
        self.partitions = [TopicPartition(topic, number) for number in sorted(partitions)]
        earliest = self.consumer.beginning_offsets(self.partitions)
        latest = self.consumer.end_offsets(self.partitions)
        self.consumer.assign(self.partitions)
        try:
            if set(positions) - {str(partition.partition) for partition in self.partitions}:
                raise ValueError(
                    "Kafka partition history changed; inspect the source before restarting"
                )
            for partition in self.partitions:
                position = positions.get(str(partition.partition), earliest[partition])
                if position < earliest[partition]:
                    raise ValueError(
                        "Saved Kafka history expired; earlier observations are unavailable"
                    )
                if position > latest[partition]:
                    raise ValueError(
                        "Kafka offsets reset; the saved position cannot be continued safely"
                    )
                self.consumer.seek(partition, position)
        except BaseException:
            self.consumer.close()
            raise
        self._pending: deque[dict[Any, list[Any]]] = deque()
        self._collecting = threading.Event()
        self._delivery_limit = delivery_limit
        self._delivered = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self.consumer, name)

    def seek_to_beginning(self, *partitions: Any) -> None:
        """Preserve the already validated explicit positions during SDK initialization."""

    def seek_to_end(self, *partitions: Any) -> None:
        """Preserve the already validated explicit positions during SDK initialization."""

    def release_startup(self) -> None:
        """Let the SDK buffer loop receive every initialization batch."""
        self._collecting.set()

    def poll(self, **parameters: Any) -> dict[Any, list[Any]]:
        """Deliver real buffered batches before requesting additional broker records."""
        if self._collecting.is_set() and self._delivered >= self._delivery_limit:
            time.sleep(0.01)
            return {}
        collecting = self._collecting.is_set()
        if collecting and self._pending:
            batches = self._pending.popleft()
        else:
            batches = self.consumer.poll(**parameters)
            if not collecting:
                if batches:
                    self._pending.append(batches)
                return {}
        result = {}
        for partition, messages in batches.items():
            wrapped = []
            for message in messages:
                if self._delivered >= self._delivery_limit:
                    break
                raw = message.value
                try:
                    value = json.loads(raw)
                    encoding = "json"
                except (ValueError, TypeError, UnicodeError):
                    import base64  # noqa: PLC0415

                    value = base64.b64encode(raw or b"").decode("ascii")
                    encoding = "base64"
                payload = {
                    "topic": message.topic,
                    "partition": message.partition,
                    "offset": message.offset,
                    "timestamp": message.timestamp,
                    "encoding": encoding,
                    "source_sha256": hashlib.sha256(raw or b"").hexdigest(),
                    "tombstone": raw is None,
                    "value": value,
                }
                wrapped.append(message._replace(value=json.dumps(payload).encode()))
                self._delivered += 1
            result[partition] = wrapped
        return result


def perform(request: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """Use the real SDK for authenticated discovery and bounded positioned reads."""
    kafka = importlib.import_module("kafka")
    KafkaConsumer, TopicPartition = kafka.KafkaConsumer, kafka.TopicPartition
    APIClient = importlib.import_module("ndp_ep").APIClient
    StreamingClient = importlib.import_module("scidx_streaming_v2").StreamingClient
    StreamHandle = importlib.import_module("scidx_streaming_v2.streams.consumer").StreamHandle

    if profile.get("endpoint_url", "").rstrip("/") != request["endpoint_url"].rstrip("/"):
        raise PermissionError("SDK broker profile is bound to a different approved Endpoint URL")
    broker = profile["kafka"]
    ep = APIClient(base_url=request["endpoint_url"], token=request.get("token"))
    ep.session.trust_env = False
    ep.get_kafka_details()  # Do not let the SDK hide an Endpoint authentication failure.
    client = StreamingClient(
        ep,
        kafka_host=broker.get("host"),
        kafka_port=broker.get("port"),
        kafka_username=broker.get("username"),
        kafka_password=broker.get("password"),
        kafka_security_protocol=broker.get("security_protocol", "SASL_SSL"),
        kafka_sasl_mechanism=broker.get("sasl_mechanism", "SCRAM-SHA-512"),
    )
    observer = None
    handle = None
    try:
        if client.kafka_connection is None:
            raise ConnectionError(
                "The SDK could not authenticate or reach the configured Kafka broker"
            )
        topic = (
            catalog_topic(ep, request["resource_id"], broker, client.kafka_host, client.kafka_port)
            if request["action"] == "open"
            else request["topic"]
        )
        descriptions = client.kafka_connection.admin_client.describe_topics([topic])
        topic_identity = str(descriptions[0].get("topic_id") or "")
        if request.get("topic_identity") and request["topic_identity"] != topic_identity:
            raise ValueError("Kafka topic identity changed; the saved history cannot be continued")
        observer = KafkaConsumer(
            bootstrap_servers=client.kafka_bootstrap,
            enable_auto_commit=False,
            allow_auto_create_topics=False,
            request_timeout_ms=5000,
            **client.kafka_security_config,
        )
        numbers = observer.partitions_for_topic(topic)
        if not numbers:
            raise ValueError("The selected Kafka topic is missing or unavailable")
        partitions = [TopicPartition(topic, number) for number in sorted(numbers)]
        earliest = {
            str(p.partition): value for p, value in observer.beginning_offsets(partitions).items()
        }
        latest = {str(p.partition): value for p, value in observer.end_offsets(partitions).items()}
        if request["action"] == "open":
            return {
                "topic": topic,
                "positions": earliest if request.get("start") == "earliest" else latest,
                "earliest_positions": earliest,
                "latest_positions": latest,
                "topic_identity": topic_identity,
                "history": "Kafka-retained records only; no earlier provider archive was queried",
            }
        consumers: list[PositionedConsumer] = []

        def factory(selected_topic: str, **configuration: Any) -> PositionedConsumer:
            adapter = PositionedConsumer(
                selected_topic, request["positions"], request["max_records"], **configuration
            )
            consumers.append(adapter)
            return adapter

        handle = StreamHandle(
            topic,
            overrides={"host": client.kafka_host, "port": client.kafka_port},
            consumer_factory=factory,
            security_config=client.kafka_security_config,
        )
        handle.start(from_beginning=True, retention_max_records=10000, poll_interval=0.05)
        consumers[0].release_startup()
        deadline = time.monotonic() + request["poll_seconds"]
        expected = sum(
            max(0, latest[number] - request["positions"].get(number, earliest[number]))
            for number in latest
        )
        target = min(request["max_records"], expected)
        while time.monotonic() < deadline:
            count = handle.summary()["total_consumed"]
            if count >= request["max_records"] or (target and count >= target):
                break
            time.sleep(0.02)
        rows = list(handle.records(limit=None))[: request["max_records"]]
        positions = {**earliest, **request["positions"]}
        retained: list[dict[str, Any]] = []
        size = 1024
        for row in rows:
            encoded = json.dumps(row).encode()
            if size + len(encoded) > request["max_bytes"]:
                if not retained:
                    raise ValueError("One Kafka record exceeds the bounded capture byte limit")
                break
            retained.append(row)
            size += len(encoded) + 2
            number = str(row["partition"])
            positions[number] = max(positions[number], row["offset"] + 1)
        return {
            "records": retained,
            "count": len(retained),
            "next_positions": positions,
            "earliest_positions": earliest,
            "latest_positions": latest,
            "topic_identity": topic_identity,
            "complete": True,
        }
    finally:
        if handle is not None:
            handle.stop()
        if observer is not None:
            observer.close()
        if client.kafka_connection is not None:
            client.kafka_connection.admin_client.close()
        ep.session.close()


def main() -> None:
    """Receive private configuration over stdin and emit a credential-free JSON result."""
    request = json.load(sys.stdin)
    watchdog = threading.Timer(float(request["runtime_seconds"]), lambda: os._exit(124))
    watchdog.daemon = True
    watchdog.start()
    profile: dict[str, Any] = {}
    expected_errors: tuple[type[Exception], ...] = (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        RuntimeError,
    )
    logging.disable(logging.CRITICAL)
    try:
        profile = json.loads(Path(request["profile_path"]).expanduser().read_text(encoding="utf-8"))
        kafka_error = importlib.import_module("kafka.errors").KafkaError
        expected_errors = (*expected_errors, kafka_error)
        with contextlib.redirect_stdout(sys.stderr):
            result = perform(request, profile)
    except ImportError:
        result = {
            "error": "sdk_unavailable",
            "message": "Install the pinned NDP streaming_v2 SDK in the selected runtime interpreter",
        }
    except expected_errors as exc:
        message = str(exc)
        for secret in [
            request.get("token"),
            profile.get("kafka", {}).get("password"),
            profile.get("kafka", {}).get("username"),
        ]:
            if secret:
                message = message.replace(str(secret), "[REDACTED]")
        result = {"error": type(exc).__name__, "message": message[:1000]}
    print(json.dumps(result))
    watchdog.cancel()


if __name__ == "__main__":
    main()
