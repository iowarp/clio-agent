"""CLIO-owned SDK runtime selection and durable Kafka reader positions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator

from clio_agent.gact.storage.models import StorageModel

PREFIX = "SDK streaming_v2."


class StreamingRuntime(StorageModel):
    """Select an installed SDK interpreter and its private broker profile on the target."""

    python: str = Field(min_length=1, max_length=4096)
    profile_path: str = Field(min_length=1, max_length=4096)

    @field_validator("python", "profile_path")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        """Keep executable and private-profile paths separate from shell syntax."""
        if any(ord(character) < 32 for character in value):
            raise ValueError("SDK runtime paths must not contain control characters")
        return value


class StreamReader(StorageModel):
    """A workspace reader owns positions, never a separate source producer."""

    id: str
    endpoint_id: str
    workspace_id: str
    principal: str
    topic: str
    positions: dict[str, int]
    cursor: str
    state: Literal["active", "stopped"] = "active"
    generation: int = 0
    created_at: str
    earliest_positions: dict[str, int]
    topic_identity: str = ""
    connection_fingerprint: str


class StreamBatch(StorageModel):
    """Retain the canonical result for one reader cursor, including an empty poll."""

    result: dict[str, Any]


def sdk_operations(runtime: StreamingRuntime | None) -> list[dict[str, Any]]:
    """Describe native SDK calls explicitly instead of impersonating Endpoint HTTP routes."""
    if runtime is None:
        return []
    fields = {
        "resource_id": {
            "type": "string",
            "minLength": 1,
            "maxLength": 160,
            "pattern": r"^[A-Za-z0-9._-]+$",
        },
        "start": {"type": "string", "enum": ["earliest", "latest"], "default": "earliest"},
        "stream_id": {"type": "string", "minLength": 1},
        "cursor": {"type": "string", "minLength": 1},
    }
    specifications = [
        (
            "open",
            "Open Kafka reader",
            "Choose a cataloged topic and earliest or latest retained position. Uses the installed NDP streaming_v2 SDK; CLIO owns continuation. Does not start the source producer.",
            ["resource_id", "start"],
            ["resource_id"],
        ),
        (
            "read",
            "Capture Kafka observations",
            "Read a finite batch from this reader's saved positions. Keep the returned continuation inside investigation state. Retrying the same cursor returns the same retained capture.",
            ["stream_id", "cursor"],
            ["stream_id"],
        ),
        (
            "list",
            "List Kafka readers",
            "List only this workspace's SDK readers and their saved positions. This does not enumerate or stop source producers.",
            [],
            [],
        ),
        (
            "stop",
            "Stop Kafka reader",
            "Stop future captures for this reader and retain its positions. The source producer continues independently.",
            ["stream_id"],
            ["stream_id"],
        ),
        (
            "resume",
            "Resume Kafka reader",
            "Allow captures from CLIO's saved positions; this does not call the SDK's history-replaying resume method.",
            ["stream_id"],
            ["stream_id"],
        ),
    ]
    return [
        {
            "id": PREFIX + action,
            "method": "SDK",
            "path": "streaming_v2." + action,
            "summary": summary,
            "description": description,
            "parameters": [
                {"name": name, "in": "query", "required": name in required, "schema": fields[name]}
                for name in names
            ],
            "body_schema": None,
            "body_required": False,
            "read_only": True,
            "transport": "streaming_v2",
        }
        for action, summary, description, names, required in specifications
    ]
