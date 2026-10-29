"""A study's record, read once from the running CLIO API and saved as JSON.

The audit, the Wordle reference and the hidden-law scorer read this snapshot, so
each report can be re-run on exactly the record it was made from:

* ``science_log``: ``GET /v1/sessions/{sid}/science-log`` (branches and entries);
* ``calls``: every tool call of every branch, from ``GET /v1/sessions/{b}/messages``
  (tool, input, result text, error, and any plugin view event);
* ``transforms``: ``GET /v1/transforms/{call_id}`` for every analysis call a claim or
  Evidence cites (script hash, input hashes, environment).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import requests


def _get(api: str, path: str) -> Any:
    response = requests.get(api.rstrip("/") + path, timeout=120)
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(block.get("text", "")) if isinstance(block, dict) else str(block)
            for block in content
        )
    return ""


def branch_calls(api: str, session: str) -> list[dict[str, Any]]:
    """Every tool call ``session`` recorded, oldest first."""

    messages = (_get(api, f"/v1/sessions/{session}/messages") or {}).get("messages") or []
    calls: dict[str, dict[str, Any]] = {}
    for message in reversed(messages):
        for part in message.get("parts") or []:
            call_id = part.get("call_id") or ""
            if part.get("type") == "tool_call" and call_id:
                calls.setdefault(
                    call_id,
                    {
                        "session": session,
                        "call_id": call_id,
                        "tool": part.get("tool_name") or "",
                        "input": part.get("input") or {},
                        "message_id": message.get("id"),
                        "created_at": message.get("created_at"),
                    },
                )
            elif part.get("type") == "tool_result" and call_id in calls:
                calls[call_id].update(
                    result_text=_text(part.get("content")),
                    is_error=bool(part.get("is_error")),
                    ui_event=(part.get("metadata") or {}).get("ui_event"),
                )
    return list(calls.values())


def analysis_calls(entries: list[dict[str, Any]]) -> set[str]:
    cited: set[str] = set()
    for entry in entries:
        fields = entry.get("fields") or {}
        if fields.get("analysis_call"):
            cited.add(str(fields["analysis_call"]))
        for claim in fields.get("claims") or []:
            if claim.get("analysis_call"):
                cited.add(str(claim["analysis_call"]))
    return {ref.rpartition("/")[2].removeprefix("call:") for ref in cited}


def snapshot(api: str, session: str) -> dict[str, Any]:
    view = _get(api, f"/v1/sessions/{session}/science-log") or {}
    branches = [row["session_id"] for row in view.get("branches") or []]
    calls = [call for branch in branches for call in branch_calls(api, branch)]
    transforms = {}
    for call_id in sorted(analysis_calls(view.get("entries") or [])):
        found = _get(api, f"/v1/transforms/{call_id}")
        transforms[call_id] = (found or {}).get("transform")
    return {
        "api": api,
        "session": session,
        "science_log": view,
        "calls": calls,
        "transforms": transforms,
    }


def load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("session", help="the study's root session (or any branch)")
    parser.add_argument("--api", default="http://127.0.0.1:8787")
    parser.add_argument("--out", required=True, help="where to write the snapshot JSON")
    args = parser.parse_args()
    data = snapshot(args.api, args.session)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(
        f"{args.out}: {len(data['calls'])} calls, {len(data['science_log'].get('entries') or [])} entries"
    )


if __name__ == "__main__":
    main()
