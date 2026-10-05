"""Submit operator-local prompts to an existing CLIO session with resumable request receipts.

Run on the operator's computer. The input must contain prompts only; evaluator
answers and injection manifests are refused. Configure the model, blueprint and
SPOTTER approval mode in CLIO first. This runner does not approve pending actions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from filelock import FileLock


def inputs(path: Path) -> list[str]:
    """Admit only the model-facing replay or broad-workflow payload."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"kind", "prompts"}:
        raise ValueError("Use a prompts-only input file, never an operator/evaluator manifest")
    if value["kind"] not in {"opal_recorded_table_replay.v1", "opal_live_export.v1"}:
        raise ValueError("Unknown OPAL qualification input kind")
    prompts = value["prompts"]
    if not isinstance(prompts, list) or not 1 <= len(prompts) <= 10:
        raise ValueError("Expected one to ten sequential prompts")
    if any(
        not isinstance(text, str) or not text.strip() or len(text) > 100_000 for text in prompts
    ):
        raise ValueError("Every prompt must be bounded, nonempty text")
    return prompts


def save(path: Path, receipt: dict[str, Any]) -> None:
    """Persist acceptance identity before sending; replace receipts atomically."""
    temporary = path.with_suffix(".tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise ValueError("Qualification receipt paths cannot be symlinks")
    temporary.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def settled_reply(messages: list[dict[str, Any]], user_id: str) -> dict[str, Any] | None:
    """Find the assistant in the accepted user's actual turn, independent of list order."""
    user = next((row for row in messages if row["id"] == user_id), None)
    if not user or not user.get("turn_id"):
        return None
    replies = [
        row
        for row in messages
        if row.get("turn_id") == user["turn_id"]
        and row.get("role") == "assistant"
        and (row.get("stop_reason") or row.get("error_info"))
    ]
    if len(replies) > 1:
        raise ValueError("The accepted turn has multiple completed assistants; inspect it in CLIO")
    return replies[0] if replies else None


def run(
    client: httpx.Client,
    *,
    endpoint: str,
    session_id: str,
    input_path: Path,
    receipt_path: Path,
    timeout: float = 1200,
) -> dict[str, Any]:
    """Run sequentially in one real conversation; resume without duplicating accepted messages."""
    lock = receipt_path.with_suffix(".lock")
    if receipt_path.is_symlink() or lock.is_symlink():
        raise ValueError("Qualification receipt paths cannot be symlinks")
    with FileLock(lock, timeout=0):
        return run_locked(
            client,
            endpoint=endpoint,
            session_id=session_id,
            input_path=input_path,
            receipt_path=receipt_path,
            timeout=timeout,
        )


def run_locked(
    client: httpx.Client,
    *,
    endpoint: str,
    session_id: str,
    input_path: Path,
    receipt_path: Path,
    timeout: float,
) -> dict[str, Any]:
    """Submit under the exclusive receipt lock, pinning the session's explicit model."""
    prompts = inputs(input_path)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        raise ValueError("Invalid session identity")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Use a plain CLIO endpoint and keep credentials in CLIO_QUALIFICATION_TOKEN"
        )

    def request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    session = request("GET", f"/v1/sessions/{session_id}")
    if session.get("approval_mode") != "spotter-ai":
        raise ValueError("Enable SPOTTER on this qualification session in CLIO first")
    model = session.get("model") or {}
    if not model.get("provider_id") or not model.get("model_id"):
        raise ValueError("Choose an explicit session model before qualification")
    identity = {
        "endpoint": endpoint.rstrip("/"),
        "session_id": session_id,
        "input_sha256": hashlib.sha256(input_path.read_bytes()).hexdigest(),
    }
    receipt = (
        json.loads(receipt_path.read_text())
        if receipt_path.exists()
        else {
            **identity,
            "model": session.get("model"),
            "turns": [],
            "complete": False,
            "capture_verified": False,
            "reviewer_verdict_verified": False,
        }
    )
    if any(receipt.get(key) != value for key, value in identity.items()):
        raise ValueError("This receipt belongs to different inputs, session or connected CLIO")
    if session.get("model") != receipt.get("model"):
        raise ValueError(
            "The session model changed; retain this evidence and start a new qualification"
        )
    if not receipt["turns"]:
        existing = request("GET", f"/v1/sessions/{session_id}/messages")["messages"]
        if any(row.get("role") == "user" for row in existing):
            raise ValueError(
                "Start with a fresh parent session so unrelated answers cannot enter the chain"
            )
    for index, prompt in enumerate(prompts):
        if index == len(receipt["turns"]):
            key = str(uuid4())
            receipt["turns"].append(
                {
                    "request": {
                        "client_message_id": key,
                        "idempotency_key": key,
                        "delivery": "start",
                        "model": model,
                        "parts": [{"type": "text", "text": prompt}],
                        "behavior": {
                            "execution_mode": "execute",
                            "confirmation_policy": "spotter-ai",
                        },
                    }
                }
            )
            save(receipt_path, receipt)
        turn = receipt["turns"][index]
        if "ack" not in turn:
            turn["ack"] = request(
                "POST", f"/v1/sessions/{session_id}/messages", json=turn["request"]
            )
            save(receipt_path, receipt)
        if turn["ack"].get("state") != "started" or turn["ack"].get("effective_model") != model:
            raise RuntimeError("Acceptance changed delivery or model; inspect the retained receipt")
        deadline = time.monotonic() + timeout
        while "reply" not in turn:
            messages = request("GET", f"/v1/sessions/{session_id}/messages")["messages"]
            reply = settled_reply(messages, turn["ack"]["message_id"])
            if reply:
                turn["reply"] = reply
                save(receipt_path, receipt)
                break
            state = request("GET", f"/v1/sessions/{session_id}")
            if state.get("status") in {"waiting_user", "waiting_permission", "error", "cancelled"}:
                raise RuntimeError(
                    "The session needs attention in CLIO; keep this receipt to resume"
                )
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Turn is still unresolved; inspect CLIO and resume using this receipt"
                )
            time.sleep(1)
        if turn["reply"].get("error_info") or turn["reply"].get("stop_reason") != "end_turn":
            raise RuntimeError("The model turn failed or stopped early; inspect its retained reply")
        print(f"Round {index + 1}/{len(prompts)} completed", flush=True)
    receipt["attention_availability"] = request(
        "GET", f"/v1/sessions/{session_id}/attention/availability"
    )
    receipt["complete"] = True
    save(receipt_path, receipt)
    return receipt


def main() -> None:
    """Execute only when explicitly invoked against the user's prepared qualification session."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=1200)
    args = parser.parse_args()
    token = os.environ.get("CLIO_QUALIFICATION_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with httpx.Client(base_url=args.endpoint.rstrip("/"), headers=headers, timeout=120) as client:
        run(
            client,
            endpoint=args.endpoint,
            session_id=args.session,
            input_path=args.input,
            receipt_path=args.receipt,
            timeout=args.timeout,
        )


if __name__ == "__main__":
    main()
