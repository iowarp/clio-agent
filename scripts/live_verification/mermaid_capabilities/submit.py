"""Prepare a real CLIO model trial, then submit only after its browser is mounted."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import httpx

from clio_agent.gact.a2ui_catalogs.builtin import workspace_catalog_id


def prepare(base: str, output: Path, stage: str, model: str, mission: Path) -> Path:
    """Create an isolated workspace/session without starting model work."""
    output.mkdir(parents=True, exist_ok=True)
    root = (output / f"{stage}-{model}").resolve()
    root.mkdir(exist_ok=True)
    with httpx.Client(base_url=base, timeout=60) as client:
        response = client.post(
            "/v1/workspaces", json={"name": f"Mermaid {stage} {model}", "root_path": str(root)}
        )
        response.raise_for_status()
        workspace = response.json()
        response = client.post(
            "/v1/sessions",
            json={
                "workspace_id": workspace["id"],
                "title": f"Mermaid {stage} {model}",
                "approval_mode": "ask",
                "model": {"provider_id": "codex", "model_id": model},
            },
        )
        response.raise_for_status()
        session = response.json()
    record: dict[str, Any] = {
        "stage": stage,
        "model": model,
        "workspace": workspace,
        "session_id": session["id"],
        "request": {
            "text": mission.read_text(encoding="utf-8"),
            "metadata": {
                "a2uiClientCapabilities": {
                    "v0.9": {"supportedCatalogIds": [workspace_catalog_id()]}
                }
            },
            "behavior": {"execution_mode": "execute", "confirmation_policy": "ask"},
        },
    }
    path = output / f"{stage}-{model}.json"
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    print(f"/workspaces/{workspace['id']}/sessions/{session['id']}")
    return path


def start(base: str, record_path: Path) -> None:
    """Submit the saved ordinary user request to the mounted real session."""
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get("submission"):
        raise ValueError("This trial was already submitted; use a fresh record.")
    with httpx.Client(base_url=base, timeout=60) as client:
        response = client.post(
            f"/v1/sessions/{record['session_id']}/messages", json=record["request"]
        )
        response.raise_for_status()
    record["submission"] = response.json()
    record_path.write_text(json.dumps(record, indent=2), encoding="utf-8")


def main() -> None:
    """Drive preparation and submission as separate, observable acceptance steps."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("prepare")
    setup.add_argument("--output", type=Path, required=True)
    setup.add_argument("--stage", required=True)
    setup.add_argument("--model", required=True)
    setup.add_argument("--mission", type=Path, required=True)
    run = commands.add_parser("start")
    run.add_argument("record", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        print(prepare(args.base_url, args.output, args.stage, args.model, args.mission))
    else:
        start(args.base_url, args.record)


if __name__ == "__main__":
    main()
