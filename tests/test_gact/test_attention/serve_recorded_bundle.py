"""Replay a collected attention bundle in the UI; this server performs no inference.

A bundle is what ``scripts/qualification/polaris/collect_attention_replay.py`` writes
(unzip it first)::

    sessions/<sid>.export.json        GET /v1/sessions/<sid>/export (children included)
    journal/<sid>.semantic.jsonl      optional: the export's own lm.call events suffice
    flowcept/decode_attention.json    the decode_attention task documents
    captures/<workflow_id>/<request id>_g<group>.safetensors
    tokenizer/                        tokenizer.json, tokenizer_config.json, chat_template.jinja

Run with an isolated CLIO_AGENT_HOME (its path must contain ``attention-replay-review``),
then connect the UI to the printed port::

    CLIO_AGENT_HOME=$PWD/attention-replay-review/home \\
      uv run python -m tests.test_gact.test_attention.serve_recorded_bundle <bundle dir>

Every exported session, and each exported child conversation (e.g. SPOTTER's, listed as its
own session), becomes one session of this isolated CLIO with its recorded transcript. Selecting answer text explains
attention from the recorded captures through the same service the live UI uses: each
capture is checked against its descriptor's size and sha256 before it is read.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import uvicorn

from clio_agent.gact.app import build_app
from clio_agent.gact.attention import routes
from clio_agent.gact.attention.lm_calls import LmCall, lm_calls_from_jsonl
from clio_agent.gact.types import Message
from tests.test_gact.test_attention._support import FakeFlowcept


def _exported(bundle: Path) -> list[tuple[dict[str, Any], str]]:
    """Each exported session and child conversation, with its parent's original id."""
    entries: list[tuple[dict[str, Any], str]] = []
    for path in sorted((bundle / "sessions").glob("*.export.json")):
        export = json.loads(path.read_text(encoding="utf-8"))
        entries.append((export, ""))
        entries += [(child, export["session"]["id"]) for child in export.get("children") or []]
    return entries


def _calls(bundle: Path, home: Path, export: dict[str, Any]) -> list[LmCall]:
    """The session's recorded ``lm.call`` records: its journal, else the export's events."""
    sid = str(export["session"]["id"])
    journal = bundle / "journal" / f"{sid}.semantic.jsonl"
    if not journal.is_file():
        journal = home / "journals" / f"{sid}.semantic.jsonl"
        journal.parent.mkdir(parents=True, exist_ok=True)
        events = [
            e for e in export.get("semantic_events") or [] if e.get("event_type") == "lm.call"
        ]
        journal.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    return lm_calls_from_jsonl(journal, sid)


def main() -> None:
    """Serve one bundle's sessions for interactive attention review."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path, help="an unzipped attention-replay-* directory")
    parser.add_argument("--port", type=int, default=18825)
    parser.add_argument("--ui-origin", default="http://127.0.0.1:4394")
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    if not (bundle / "sessions").is_dir() or not (bundle / "captures").is_dir():
        raise SystemExit(f"{bundle} is not an unzipped attention replay bundle")
    root = Path(os.environ["CLIO_AGENT_HOME"]).resolve()
    if "attention-replay-review" not in root.parts:
        raise ValueError("Use a dedicated attention-replay-review state directory")
    root.mkdir(parents=True, exist_ok=True)
    os.chdir(root.parent)
    os.environ["CLIO_PROVENANCE_ATTENTION"] = "1"
    os.environ["CLIO_PROVENANCE_ATTENTION_FILES_DIR"] = str(bundle / "captures")
    os.environ["CLIO_PROVENANCE_ATTENTION_TOKENIZER"] = str(bundle / "tokenizer")
    os.environ["CLIO_GACT_CORS_ORIGINS"] = args.ui_origin
    app = build_app(agent=None, sessions_path=root / "sessions.json")

    calls: dict[str, list[LmCall]] = {}
    for export, parent in _exported(bundle):
        original = export["session"]
        # A child (SPOTTER) is listed on its own: its delegation record is not replayed.
        of = f" of {parent}" if parent else ""
        title = f"{original.get('title') or original['id']} (replay {original['id']}{of})"
        session = next((s for s in app.state.sessions.list() if s.title == title), None)
        if session is None:
            session = app.state.sessions.create(workspace_id="ws_default", title=title)
        messages = [Message.model_validate(row) for row in export.get("messages") or []]
        for message in messages:
            message.session_id = session.id
        app.state.messages[session.id] = messages
        calls[session.id] = [
            replace(call, session_id=session.id) for call in _calls(bundle, root, export)
        ]
        print(
            f"{session.id}  {title}: {len(messages)} messages, {len(calls[session.id])} model calls",
            flush=True,
        )

    tasks = json.loads((bundle / "flowcept" / "decode_attention.json").read_text(encoding="utf-8"))
    flowcept = FakeFlowcept(tasks)
    routes.attention_capture_enabled = lambda: True
    routes.attention_store = lambda app: routes.AttentionStore(flowcept)
    routes.session_lm_calls = lambda app, sid: calls.get(sid, [])
    # No live writer: the overrides above read the bundle.
    app.state.semantic_trace_backend = SimpleNamespace()
    print(f"{len(tasks)} decode_attention documents; UI endpoint http://127.0.0.1:{args.port}")
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
