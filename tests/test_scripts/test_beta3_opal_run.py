"""Qualification runner preserves turn identity, retries and the evaluator boundary."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from filelock import FileLock, Timeout

from scripts.qualification.beta3_opal_run import inputs, run, settled_reply

MODEL = {"provider_id": "qualified-runtime", "model_id": "qualified-model", "variant": ""}


def input_file(tmp_path: Path) -> Path:
    """Create model-facing test prompts only."""
    path = tmp_path / "inputs.json"
    path.write_text(
        json.dumps({"kind": "opal_live_export.v1", "prompts": ["Question one", "Question two"]})
    )
    return path


def test_lost_acceptance_reuses_identity_and_follows_turn_not_message_order(tmp_path: Path) -> None:
    submitted: list[dict[str, Any]] = []
    accepted: dict[str, dict[str, Any]] = {}
    fail_response = True

    def server(request: httpx.Request) -> httpx.Response:
        nonlocal fail_response
        if request.url.path.endswith("attention/availability"):
            return httpx.Response(200, json={"available": False})
        if request.method == "POST":
            body = json.loads(request.content)
            submitted.append(body)
            key = body["idempotency_key"]
            accepted[key] = body
            if fail_response:
                fail_response = False
                raise httpx.ReadTimeout("Acceptance response lost", request=request)
            return httpx.Response(
                202, json={"message_id": key, "state": "started", "effective_model": MODEL}
            )
        if request.url.path.endswith("/messages"):
            rows: list[dict[str, Any]] = [
                {
                    "id": "wrong-reply",
                    "role": "assistant",
                    "turn_id": "foreign",
                    "stop_reason": "end_turn",
                }
            ]
            for key in reversed(accepted):
                rows.extend(
                    [
                        {
                            "id": "reply-" + key,
                            "role": "assistant",
                            "turn_id": key,
                            "stop_reason": "end_turn",
                        },
                        {"id": key, "role": "user", "turn_id": key},
                    ]
                )
            return httpx.Response(200, json={"messages": rows})
        return httpx.Response(200, json={"approval_mode": "spotter-ai", "model": MODEL})

    with httpx.Client(base_url="http://clio.test", transport=httpx.MockTransport(server)) as client:
        kwargs = {
            "endpoint": "http://clio.test",
            "session_id": "parent",
            "input_path": input_file(tmp_path),
            "receipt_path": tmp_path / "receipt.json",
        }
        with pytest.raises(httpx.ReadTimeout):
            run(client, **kwargs)
        receipt = run(client, **kwargs)
        assert submitted[0] == submitted[1]
        assert submitted[2]["idempotency_key"] != submitted[0]["idempotency_key"]
        assert len(accepted) == 2
        assert all(
            turn["reply"]["turn_id"] == turn["ack"]["message_id"] for turn in receipt["turns"]
        )
        assert receipt["complete"]
        assert not receipt["capture_verified"] and not receipt["reviewer_verdict_verified"]
        assert not receipt["attention_availability"]["available"]
        run(client, **kwargs)
        assert len(submitted) == 3


@pytest.mark.parametrize("status", ["waiting_permission", "error"])
def test_pending_action_never_auto_approved_or_resent(tmp_path: Path, status: str) -> None:
    posted: list[dict[str, Any]] = []

    def server(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posted.append(json.loads(request.content))
            return httpx.Response(
                202, json={"message_id": "accepted", "state": "started", "effective_model": MODEL}
            )
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": []})
        return httpx.Response(
            200, json={"approval_mode": "spotter-ai", "model": MODEL, "status": status}
        )

    with httpx.Client(base_url="http://clio.test", transport=httpx.MockTransport(server)) as client:
        kwargs = {
            "endpoint": "http://clio.test",
            "session_id": "parent",
            "input_path": input_file(tmp_path),
            "receipt_path": tmp_path / "receipt.json",
        }
        for _ in range(2):
            with pytest.raises(RuntimeError, match="needs attention"):
                run(client, **kwargs)
        assert len(posted) == 1
        assert not json.loads((tmp_path / "receipt.json").read_text())["complete"]


def test_reviewer_and_input_boundaries_fail_before_post(tmp_path: Path) -> None:
    path = input_file(tmp_path)
    path.write_text(json.dumps({"kind": "opal_operator_manifest.v1", "correct_answer": "private"}))
    with pytest.raises(ValueError, match="prompts-only"):
        inputs(path)
    path = input_file(tmp_path)
    requests: list[str] = []

    def server(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(200, json={"approval_mode": "ask", "model": MODEL})

    with httpx.Client(base_url="http://clio.test", transport=httpx.MockTransport(server)) as client:
        with pytest.raises(ValueError, match="Enable SPOTTER"):
            run(
                client,
                endpoint="http://clio.test",
                session_id="parent",
                input_path=path,
                receipt_path=tmp_path / "receipt.json",
            )
    assert requests == ["GET"]


def test_receipt_lock_prevents_concurrent_submission(tmp_path: Path) -> None:
    with FileLock(tmp_path / "receipt.lock"), httpx.Client() as client:
        with pytest.raises(Timeout):
            run(
                client,
                endpoint="http://clio.test",
                session_id="parent",
                input_path=input_file(tmp_path),
                receipt_path=tmp_path / "receipt.json",
            )


def test_ambiguous_completed_turn_refuses_to_guess() -> None:
    rows = [
        {"id": "user", "role": "user", "turn_id": "turn"},
        {"id": "a", "role": "assistant", "turn_id": "turn", "stop_reason": "end_turn"},
        {"id": "b", "role": "assistant", "turn_id": "turn", "stop_reason": "end_turn"},
    ]
    with pytest.raises(ValueError, match="multiple completed"):
        settled_reply(rows, "user")
