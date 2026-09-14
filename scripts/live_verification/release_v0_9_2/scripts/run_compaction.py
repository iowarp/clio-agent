"""Drive and record the release-specific progressive-compaction live gate.

This wrapper targets an already-running release candidate. Browser screenshots
are captured separately because this script records backend evidence only.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from scripts.live_verification._common import (  # noqa: E402
    allow_all,
    bind_provider,
    client,
    create_session,
    create_workspace,
    post_message,
    session_messages,
    wait_turn,
)

DEFAULT_OUT = ROOT / "out" / "live-verification" / "release_v0_9_2" / "compaction"
DEFAULT_BASE = "http://127.0.0.1:8787"


def _dump(out: Path, name: str, value: Any) -> None:
    """Write one timestamped evidence document."""

    out.mkdir(parents=True, exist_ok=True)
    payload = {"recorded_at": datetime.now(UTC).isoformat(), "value": value}
    (out / f"{name}.json").write_text(
        json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8"
    )


def _load_ids(out: Path) -> tuple[str, str]:
    """Return the recorded workspace and session identifiers."""

    value = json.loads((out / "ids.json").read_text(encoding="utf-8"))
    return str(value["workspace_id"]), str(value["session_id"])


def _parts(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten transcript parts in wire order."""

    return [part for message in messages for part in message.get("parts", [])]


def init(base: str, out: Path) -> None:
    """Bind Codex/Luna and create a fresh qualification session."""

    call = client(base)
    provider = bind_provider(call, provider="codex", model="gpt-5.6-luna")
    allow_all(call)
    workspace_root = out.parent / "workspace-compaction"
    workspace_id = create_workspace(call, "CLIO v0.9.2 qualification", workspace_root)
    session_id = create_session(call, workspace_id, "v0.9.2 progressive compaction qualification")
    out.mkdir(parents=True, exist_ok=True)
    (out / "ids.json").write_text(
        json.dumps({"workspace_id": workspace_id, "session_id": session_id}, indent=2) + "\n",
        encoding="utf-8",
    )
    _dump(out, "provider", provider)
    print(json.dumps({"workspace_id": workspace_id, "session_id": session_id}))


def turn(base: str, out: Path, text: str, label: str) -> None:
    """Post one turn, wait for completion, and snapshot the transcript."""

    call = client(base)
    workspace_id, session_id = _load_ids(out)
    posted = post_message(call, session_id, text)
    status = wait_turn(call, workspace_id, session_id, max_elapsed=300.0)
    messages = session_messages(call, session_id)
    _dump(out, label, {"posted": posted, "status": status, "messages": messages})
    print(json.dumps({"status": status, "message_count": len(messages)}))


def preferences(base: str, out: Path, *, automatic: bool, pct: float) -> None:
    """Set the complete per-session compaction policy in one request."""

    call = client(base)
    _, session_id = _load_ids(out)
    value = call(
        "PATCH",
        f"/v1/sessions/{session_id}/context/preferences",
        {"automatic_compaction": automatic, "autocompact_pct": pct},
    )
    _dump(out, f"preferences-{'on' if automatic else 'off'}-{pct:g}", value)
    print(json.dumps(value))


def compact(base: str, out: Path, label: str) -> None:
    """Perform one manual compaction and capture its checkpoint response."""

    call = client(base)
    _, session_id = _load_ids(out)
    value = call("POST", f"/v1/sessions/{session_id}/compact", {})
    _dump(out, label, {"response": value, "messages": session_messages(call, session_id)})
    print(json.dumps(value))


def snapshot(base: str, out: Path, label: str) -> None:
    """Record transcript, frames, session metadata, and memory events."""

    call = client(base)
    workspace_id, session_id = _load_ids(out)
    sessions = call("GET", f"/v1/sessions?workspace_id={workspace_id}").get("sessions", [])
    session = next(row for row in sessions if row.get("id") == session_id)
    value = {
        "session": session,
        "messages": session_messages(call, session_id),
        "frames": call("GET", f"/v1/sessions/{session_id}/context/frames").get("frames", []),
        "memory_events": call("GET", f"/v1/sessions/{session_id}/memory/events").get("events", []),
    }
    _dump(out, label, value)
    print(json.dumps({"message_count": len(value["messages"]), "status": session.get("status")}))


def assert_final(base: str, out: Path) -> None:
    """Require all markers and exactly three append-only checkpoints."""

    call = client(base)
    _, session_id = _load_ids(out)
    messages = session_messages(call, session_id)
    wire = json.dumps(messages, sort_keys=True)
    compactions = [part for part in _parts(messages) if part.get("type") == "compaction"]
    automatic = [part for part in compactions if part.get("auto") is True]
    checks = {
        "early_marker_retained": "V092-EARLY-CEDAR" in wire,
        "middle_marker_retained": "V092-MIDDLE-ORBIT" in wire,
        "late_marker_retained": "V092-LATE-EMBER" in wire,
        "three_compaction_checkpoints": len(compactions) == 3,
        "one_automatic_checkpoint": len(automatic) == 1,
        "all_checkpoints_have_covered_ids": all(
            bool(part.get("compacted_message_ids")) for part in compactions
        ),
        "message_ids_unique": len({row.get("id") for row in messages}) == len(messages),
    }
    result = {
        "checks": checks,
        "pass": all(checks.values()),
        "message_count": len(messages),
        "compaction_count": len(compactions),
    }
    _dump(out, "final-assertions", result)
    print(json.dumps(result, indent=2))
    if not result["pass"]:
        raise SystemExit(1)


def main() -> None:
    """Dispatch one bounded live-gate operation."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    turn_parser = sub.add_parser("turn")
    turn_parser.add_argument("--text", required=True)
    turn_parser.add_argument("--label", required=True)
    prefs_parser = sub.add_parser("preferences")
    prefs_parser.add_argument("--automatic", choices=("true", "false"), required=True)
    prefs_parser.add_argument("--pct", type=float, required=True)
    compact_parser = sub.add_parser("compact")
    compact_parser.add_argument("--label", required=True)
    snapshot_parser = sub.add_parser("snapshot")
    snapshot_parser.add_argument("--label", required=True)
    sub.add_parser("assert-final")
    args = parser.parse_args()
    if args.command == "init":
        init(args.base, args.out)
    elif args.command == "turn":
        turn(args.base, args.out, args.text, args.label)
    elif args.command == "preferences":
        preferences(args.base, args.out, automatic=args.automatic == "true", pct=args.pct)
    elif args.command == "compact":
        compact(args.base, args.out, args.label)
    elif args.command == "snapshot":
        snapshot(args.base, args.out, args.label)
    else:
        assert_final(args.base, args.out)


if __name__ == "__main__":
    main()
