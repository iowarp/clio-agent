#!/usr/bin/env python3
"""Live PDF-skill acceptance: @-referenced and attached PDFs, three agents.

Proves the ``work-with-pdfs`` skill end to end on a real model, on three
agents:

* ``builtin-main``: what a session runs when no Agent Blueprint is active (a
  fresh deployment's new session); CLIO's code-shipped main declares the
  built-in ``work-with-pdfs`` skill and ``view_image``.
* ``base-agent``: the marketplace default agent; CLIO auto-declares its
  built-in skills on this default blueprint's root expert.
* ``factorio-flat``: a marketplace pack that ships its own copy of the skill.

Each is driven with the PDF supplied two ways, with exactly the wire shapes
gact-tui sends:

* **@-reference**: the composer's reference picker lists workspace files via
  ``GET /v1/workspaces/{ws}/references?kinds=workspace_file`` and sends the
  chosen row as ``{"type": "context_ref", "ref_kind", "ref_id", "label",
  "revision"}`` (``web/src/lib/composer-reference-domain.ts``).
* **attachment**: ``upload-workspace-resources.ts`` creates a resource
  (``POST .../resources``), appends bytes (``PATCH .../content`` with
  ``Upload-Offset``), waits for ``state == "ready"``, and sends
  ``{"type": "resource_ref", "resource_id", "resource_revision", "name"}``.

Each cell gets its own workspace and a freshly generated PDF with two
unguessable canaries: one in the text layer (page 1) and one that exists only
as raster pixels (page 2), so answering both needs the skill's text
conversion AND its rendered-page + ``view_image`` path.

A cell passes when the final answer quotes both canaries, the turn ended
cleanly, and the evidence route is identifiable. The route is recorded, not
prescribed (owner ruling): a natively delivered attachment, ``view_pdf`` on a
PDF-capable model, or the skill's conversion + ``view_image`` path.

Nothing is steered: the prompt is ordinary user wording and never names the
skill or its tools.

``--plumbing-only`` exercises everything except the model: server boot,
fixture generation, pack install/activation, reference listing, upload
custody, and resolved agent tools.

Run under a private real CTE daemon (never the ARC local store)::

    uv run python scripts/live_verification/run_with_private_cte.py \\
        scripts/live_verification/leg_pdf_skills.py --provider claude_code --model claude-sonnet-5
"""

from __future__ import annotations

import argparse
import io
import json
import secrets
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as common  # noqa: E402

DEFAULT_MARKETPLACE = common.REPO / "external" / "clio-agent-marketplace"
PROMPT = (
    'What does this PDF report? Quote the sentence that starts with "Finding:" '
    "exactly, and tell me what the label on the second page says."
)
AGENTS = ("builtin-main", "base-agent", "factorio-flat")
DELIVERIES = ("at_reference", "attachment")
UPLOAD_CHUNK_BYTES = 256 * 1024


@dataclass(frozen=True)
class Fixture:
    """One generated PDF and the canaries only its content reveals."""

    path: Path
    text_canary: str
    image_canary: str


def _token(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3).upper()}"


def make_fixture(path: Path) -> Fixture:
    """Write a two-page PDF: a text-layer finding and a raster-only label.

    Page 2's label is drawn into a PNG first and embedded as an image, so no
    text layer carries it; only rendering the page and looking at it reveals
    the canary.
    """

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    text_canary = _token("ORCHID")
    image_canary = _token("KESTREL")
    path.parent.mkdir(parents=True, exist_ok=True)

    raster = io.BytesIO()
    label = plt.figure(figsize=(6, 2), dpi=150)
    label.text(0.5, 0.6, "VALVE SETPOINT LABEL", ha="center", fontsize=16, weight="bold")
    label.text(0.5, 0.25, image_canary, ha="center", fontsize=28, family="monospace")
    label.savefig(raster, format="png")
    plt.close(label)
    raster.seek(0)
    pixels = plt.imread(raster)

    with PdfPages(path) as pdf:
        page = plt.figure(figsize=(8.5, 11))
        page.text(0.1, 0.9, "Calibration Brief", fontsize=22, weight="bold")
        page.text(
            0.1,
            0.8,
            f"Finding: the calibrated sample drifted 0.37 mm under protocol {text_canary}.",
            fontsize=11,
        )
        page.text(0.1, 0.74, "Page 2 shows the valve setpoint label.", fontsize=11)
        pdf.savefig(page)
        plt.close(page)

        page = plt.figure(figsize=(8.5, 11))
        axes = page.add_axes((0.1, 0.55, 0.8, 0.3))
        axes.imshow(pixels)
        axes.axis("off")
        pdf.savefig(page)
        plt.close(page)
    return Fixture(path=path, text_canary=text_canary, image_canary=image_canary)


def post_parts(call: Callable[..., Any], sid: str, parts: list[dict[str, Any]]) -> dict[str, Any]:
    """POST a message with explicit parts, as the web composer does."""

    return call("POST", f"/v1/sessions/{sid}/messages", {"parts": parts}, ok=(200, 201, 202))


def reference_part(call: Callable[..., Any], wsid: str, rel_path: str) -> dict[str, Any]:
    """Pick ``rel_path`` from the composer's workspace-file reference listing."""

    def _find() -> dict[str, Any] | None:
        rows = call(
            "GET",
            f"/v1/workspaces/{wsid}/references",
            params={"kinds": "workspace_file", "q": Path(rel_path).name},
        ).get("references", [])
        for row in rows:
            if str(row.get("id") or "").replace("\\", "/").endswith(rel_path):
                return row
        return None

    row = common.expanding_wait(_find, what=f"workspace reference for {rel_path}", max_elapsed=60)
    if row is None:
        raise RuntimeError(f"{rel_path} never appeared in the workspace reference listing")
    part: dict[str, Any] = {
        "type": "context_ref",
        "ref_kind": row["kind"],
        "ref_id": row["id"],
        "label": row.get("label") or Path(rel_path).name,
    }
    if row.get("revision"):
        part["revision"] = row["revision"]
    return part


def attachment_part(call: Callable[..., Any], base: str, wsid: str, pdf: Path) -> dict[str, Any]:
    """Upload ``pdf`` through resource custody and return its ``resource_ref``."""

    import hashlib

    import requests

    data = pdf.read_bytes()
    created = call(
        "POST",
        f"/v1/workspaces/{wsid}/resources",
        {
            "name": pdf.name,
            "size": len(data),
            "media_type": "application/pdf",
            "client_upload_id": hashlib.sha256(data).hexdigest(),
        },
    )
    resource_id = str(created["id"])
    for offset in range(int(created.get("received_size") or 0), len(data), UPLOAD_CHUNK_BYTES):
        chunk = data[offset : offset + UPLOAD_CHUNK_BYTES]
        response = requests.patch(
            f"{base}/v1/workspaces/{wsid}/resources/{resource_id}/content",
            data=chunk,
            headers={
                "Content-Type": "application/offset+octet-stream",
                "Upload-Offset": str(offset),
            },
            timeout=120,
        )
        if response.status_code not in (200, 204):
            raise RuntimeError(
                f"upload chunk at {offset} -> {response.status_code}: {response.text}"
            )

    def _ready() -> dict[str, Any] | None:
        row = call("GET", f"/v1/workspaces/{wsid}/resources/{resource_id}")
        state = str(row.get("state") or "")
        if state in {"failed", "quarantined"}:
            raise RuntimeError(f"resource {resource_id} ended {state}: {row.get('failure')}")
        return row if state == "ready" else None

    record = common.expanding_wait(_ready, what=f"resource {resource_id} ready", max_elapsed=120)
    if record is None:
        raise RuntimeError(f"resource {resource_id} never became ready")
    if int(record.get("received_size") or 0) < len(data):
        raise RuntimeError(f"resource {resource_id} holds fewer bytes than were uploaded")
    return {
        "type": "resource_ref",
        "resource_id": resource_id,
        "resource_revision": str(record.get("revision") or 1),
        "name": pdf.name,
    }


def _row_text(row: dict[str, Any]) -> str:
    return json.dumps(row, default=str)


def _native_attachment_delivered(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        if message.get("role") != "user":
            continue
        for part in message.get("parts") or []:
            delivery = (part.get("metadata") or {}).get("delivery") or {}
            if part.get("type") == "resource_ref" and delivery.get("representation") == "native":
                return True
    return False


def evaluate(messages: list[dict[str, Any]], fixture: Fixture, status: str) -> dict[str, Any]:
    """Score one cell on the evidence the answer rests on, and record the route.

    A cell passes when the final answer quotes BOTH canaries (the text-layer
    finding and the raster-only label) and the turn ended cleanly. How the
    model got there is recorded, not prescribed: a natively delivered
    attachment, ``view_pdf``, or the skill's conversion + ``view_image`` path
    are all legitimate on a PDF-capable model.
    """

    calls = common.tool_calls_from_messages(messages)
    skill_calls = [
        c
        for c in calls
        if str(c.get("name") or "").endswith("load_skill") and "work-with-pdfs" in _row_text(c)
    ]
    prepare_ok = [c for c in calls if "prepare_pdf.py" in _row_text(c) and common.tool_call_ok(c)]
    view_pdf_ok = [
        c for c in calls if str(c.get("name") or "").endswith("view_pdf") and common.tool_call_ok(c)
    ]
    view_image_ok = [
        c
        for c in calls
        if str(c.get("name") or "").endswith("view_image") and common.tool_call_ok(c)
    ]
    native = _native_attachment_delivered(messages)
    if view_pdf_ok:
        route = "view_pdf"
    elif prepare_ok:
        route = "skill_conversion"
    elif native:
        route = "native_attachment"
    else:
        route = "none_evidenced"
    assistant = [m for m in messages if m.get("role") == "assistant"]
    final = assistant[-1] if assistant else {}
    answer = " ".join(
        str(p.get("text") or "") for p in final.get("parts") or [] if p.get("type") == "text"
    )
    error_parts = [p for p in final.get("parts") or [] if p.get("type") == "error"]
    checks = {
        "text_canary_in_answer": fixture.text_canary in answer,
        "image_canary_in_answer": fixture.image_canary in answer,
        "clean_turn": status in {"idle", "completed"} and not error_parts,
        "evidenced_route": route != "none_evidenced",
    }
    return {
        "checks": checks,
        "pass": all(checks.values()),
        "route": route,
        "skill_loaded": bool(skill_calls),
        "turn_status": status,
        "tool_sequence": [str(c.get("name") or "") for c in calls],
        "view_pdf_calls": len(view_pdf_ok),
        "view_image_calls": len(view_image_ok),
        "prepare_pdf_calls": len(prepare_ok),
        "answer": answer,
        "error_parts": error_parts,
    }


def run_cell(
    call: Callable[..., Any],
    base: str,
    root: Path,
    agent: str,
    delivery: str,
    marketplace: Path,
    plumbing_only: bool,
    turn_timeout_s: float,
) -> dict[str, Any]:
    """Drive one (agent, delivery) cell in its own workspace."""

    name = f"{agent}-{delivery}"
    ws_dir = root / name
    if ws_dir.exists():
        shutil.rmtree(ws_dir)
    fixture = make_fixture(ws_dir / "docs" / "calibration-brief.pdf")
    wsid = common.create_workspace(call, f"pdf-{name}", ws_dir)
    sid = common.create_session(call, wsid, f"pdf-{name}")
    cell: dict[str, Any] = {
        "cell": name,
        "workspace_id": wsid,
        "session_id": sid,
        "fixture": {
            "path": str(fixture.path),
            "text_canary": fixture.text_canary,
            "image_canary": fixture.image_canary,
        },
    }
    if agent != "builtin-main":
        installed = common.install_blueprint(call, marketplace / agent, wsid)
        common.activate_blueprint(call, sid, str(installed.get("id") or ""))
    session = call("GET", f"/v1/sessions/{sid}")
    cell["session_metadata"] = session.get("metadata") or {}
    cell["resolved_agent_tools"] = common.resolved_agent_tools(call, sid, workspace_id=wsid)

    if delivery == "at_reference":
        ref = reference_part(call, wsid, "docs/calibration-brief.pdf")
    else:
        ref = attachment_part(call, base, wsid, fixture.path)
    cell["reference_part"] = ref
    if plumbing_only:
        cell["pass"] = True
        return cell

    started = time.monotonic()
    post_parts(call, sid, [{"type": "text", "text": PROMPT}, ref])
    status = common.wait_turn(call, wsid, sid, max_elapsed=turn_timeout_s)
    cell["elapsed_s"] = round(time.monotonic() - started, 1)
    messages = common.session_messages(call, sid)
    common.dump_json(root / f"{name}.messages.json", messages)
    cell.update(evaluate(messages, fixture, status))
    return cell


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18997)
    parser.add_argument("--provider", default="claude_code")
    parser.add_argument("--model", default="claude-sonnet-5")
    parser.add_argument("--marketplace", default=str(DEFAULT_MARKETPLACE))
    parser.add_argument("--root", default=str(common.OUT_ROOT / "pdf-skills"))
    parser.add_argument("--agents", default=",".join(AGENTS))
    parser.add_argument("--deliveries", default=",".join(DELIVERIES))
    parser.add_argument("--turn-timeout-s", type=float, default=1200.0)
    parser.add_argument("--plumbing-only", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    out_path = root / "verdict.json"
    marketplace = Path(args.marketplace).resolve()
    agents = [a for a in args.agents.split(",") if a]
    deliveries = [d for d in args.deliveries.split(",") if d]
    verdict: dict[str, Any] = {
        "leg": "pdf_skills",
        "prompt": PROMPT,
        "provider": {"provider": args.provider, "model": args.model},
        "plumbing_only": args.plumbing_only,
        "marketplace": str(marketplace),
        "cells": [],
    }
    if not common.port_is_free(args.port):
        common.write_verdict(
            out_path, {**verdict, "error": f"port {args.port} busy", "pass": False}
        )
        return 1

    base = f"http://127.0.0.1:{args.port}"
    proc = common.boot_server(
        args.port,
        cwd=root,
        sse_log=root / "sse.log",
        extra_env={"CLIO_USER_DIR": str(root / "user-config"), "CLIO_ALLOWED_ROOTS": str(root)},
    )
    try:
        call = common.client(base)
        if not common.wait_health(call):
            common.write_verdict(
                out_path, {**verdict, "error": "server never healthy", "pass": False}
            )
            return 1
        common.allow_all(call)
        if not args.plumbing_only:
            verdict["provider_state"] = common.bind_provider(
                call, provider=args.provider, model=args.model
            )
        for agent in agents:
            for delivery in deliveries:
                try:
                    cell = run_cell(
                        call,
                        base,
                        root,
                        agent,
                        delivery,
                        marketplace,
                        args.plumbing_only,
                        args.turn_timeout_s,
                    )
                except Exception as exc:  # noqa: BLE001 - recorded per cell, run continues
                    cell = {
                        "cell": f"{agent}-{delivery}",
                        "pass": False,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                verdict["cells"].append(cell)
                print(
                    f"[cell] {cell['cell']}: pass={cell.get('pass')} route={cell.get('route')} checks={cell.get('checks')}",
                    flush=True,
                )
                common.dump_json(out_path, verdict)
        verdict["pass"] = bool(verdict["cells"]) and all(c.get("pass") for c in verdict["cells"])
        common.write_verdict(out_path, verdict)
        return 0 if verdict["pass"] else 1
    finally:
        common.terminate_server(proc)


if __name__ == "__main__":
    raise SystemExit(main())
