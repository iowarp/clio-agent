"""Collect what an offline attention replay of the OPAL sessions needs (no GPU, no vLLM).

Run on the compute node while CLIO and Flowcept are still up, after step 07 exported
the sessions::

    .venv/bin/python -I scripts/qualification/polaris/collect_attention_replay.py \\
        --job 7795352 --session chain_A=sess_x --session chain_B=sess_y --session broad=sess_z

Writes ``$ROOT/exports/attention-replay-<job>/``:

- ``sessions/<sid>.export.json``      the session export (``GET /v1/sessions/<sid>/export``)
- ``journal/<sid>.semantic.jsonl``    each session's model-call journal
- ``flowcept/decode_attention.json``  every Flowcept ``decode_attention`` task (all, unfiltered)
- ``captures/<workflow_id>/<request id>_g<group>.safetensors``  the sessions' captures, byte-exact
- ``tokenizer/``                      tokenizer.json, tokenizer_config.json, chat_template.jinja
- ``metadata.json``                   commits, model, job, session map, and the descriptor check

The check: every descriptor whose request id belongs to a collected session must resolve
to a copied capture with the descriptor's byte count and sha256. Mismatches are reported,
never repaired. No credential is written: Flowcept's settings are read, not copied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

ROOT = Path(
    os.environ.get(
        "ROOT", "/lus/eagle/projects/GenesisHackathonOct26/jcernuda/clio-polaris-2026-10"
    )
)
CLIO = os.environ.get("CLIO_ENDPOINT", "http://127.0.0.1:8100")
TOKENIZER_FILES = ("tokenizer.json", "tokenizer_config.json", "chat_template.jinja")


def _get(path: str) -> Any:
    with urllib.request.urlopen(CLIO + path, timeout=300) as response:
        return json.load(response)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _commit(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
    ).stdout.strip()


def _provenance_config() -> dict[str, Any]:
    import yaml  # noqa: PLC0415

    return yaml.safe_load((ROOT / "state" / "config" / "config.yaml").read_text())["provenance"]


def _decode_attention_tasks(settings_path: Path) -> list[dict[str, Any]]:
    """Every decode_attention task from this job's Flowcept MongoDB (credentials stay in memory)."""
    from bson import json_util  # noqa: PLC0415
    from pymongo import MongoClient  # noqa: PLC0415

    settings = json.loads(settings_path.read_text())  # Flowcept writes this file as JSON
    mongo = settings["databases"]["mongodb"]
    client = MongoClient(mongo["uri"], serverSelectionTimeoutMS=15000)
    try:
        rows = list(client[mongo["db"]]["tasks"].find({"activity_id": "decode_attention"}))
    finally:
        client.close()
    return json.loads(json_util.dumps(rows))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--job", required=True)
    parser.add_argument(
        "--session", action="append", default=[], help="label=sid (chain_A, chain_B, broad)"
    )
    args = parser.parse_args()
    sessions = dict(item.split("=", 1) for item in args.session)
    out = ROOT / "exports" / f"attention-replay-{args.job}"
    for sub in ("sessions", "journal", "flowcept", "captures", "tokenizer"):
        (out / sub).mkdir(parents=True, exist_ok=True)

    # 1. Session exports (step 07 wrote them; fetch fresh if missing).
    for sid in sessions.values():
        target = out / "sessions" / f"{sid}.export.json"
        exported = ROOT / "evidence" / "sessions" / args.job / f"{sid}.json"
        if exported.is_file():
            shutil.copy2(exported, target)
        else:
            target.write_text(json.dumps(_get(f"/v1/sessions/{sid}/export")))

    # 2. Model-call journals.
    journals: dict[str, str] = {}
    for sid in sessions.values():
        hits = sorted((ROOT / "state").rglob(f"{sid}.semantic.jsonl"))
        if hits:
            shutil.copy2(hits[0], out / "journal" / hits[0].name)
            journals[sid] = str(hits[0])

    # 3. All decode_attention descriptors.
    provenance = _provenance_config()
    settings_path = Path(provenance["agentic"]["flowcept"]["settings_path"])
    tasks = _decode_attention_tasks(settings_path)
    (out / "flowcept" / "decode_attention.json").write_text(json.dumps(tasks, indent=1))

    # 4. Capture files of the collected sessions, byte-exact, in their
    #    <workflow_id>/<request id>_g<group> layout.
    files_dir = Path(provenance["attention"]["files_dir"])
    response_ids: dict[str, str] = {}
    for sid in sessions.values():
        journal = out / "journal" / f"{sid}.semantic.jsonl"
        if not journal.is_file():
            continue
        for line in journal.read_text().splitlines():
            event = json.loads(line) if line.strip() else {}
            rid = str((event.get("payload") or {}).get("response_id") or "")
            if event.get("event_type") == "lm.call" and rid:
                response_ids[rid] = sid
    owned: list[tuple[dict[str, Any], str]] = []
    for task in tasks:
        request_id = str((task.get("used") or {}).get("request_id") or "")
        sid = next((s for rid, s in response_ids.items() if request_id.startswith(rid)), "")
        if sid:
            owned.append((task, sid))
    copied = 0
    for task, _ in owned:
        uri = (task.get("attention_stats") or {}).get("uri") or ""
        source = Path(unquote(urlparse(uri).path)) if uri else None
        if source is None or not source.is_file():
            continue
        target = out / "captures" / source.relative_to(files_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(source, target)
            copied += 1

    # 5. Tokenizer files of the served snapshot.
    vllm = json.loads((ROOT / "evidence" / f"job-{args.job}" / "06-vllm.json").read_text())
    snapshot = Path(str(vllm.get("model_path") or vllm.get("model") or ""))
    if not snapshot.is_dir():
        snapshot = next((ROOT / "hf" / "hub").glob("ibm-granite--granite-4.2-8b--*"))
    for name in TOKENIZER_FILES:
        if (snapshot / name).is_file():
            shutil.copy2(snapshot / name, out / "tokenizer" / name)

    # The check: descriptors of the collected sessions' model calls vs the copied captures.
    checks: list[dict[str, Any]] = []
    for task, sid in owned:
        stats = task.get("attention_stats") or {}
        uri = stats.get("uri") or ""
        row: dict[str, Any] = {"session": sid, "task_id": task.get("task_id"), "uri": uri}
        if not uri:
            row["result"] = "no_file (" + str(stats.get("error") or "no uri") + ")"
        else:
            relative = Path(unquote(urlparse(uri).path)).relative_to(files_dir)
            local = out / "captures" / relative
            if not local.is_file():
                row["result"] = "missing"
            elif local.stat().st_size != int(stats.get("bytes") or -1):
                row["result"] = f"size_mismatch ({local.stat().st_size} != {stats.get('bytes')})"
            elif _sha256(local) != stats.get("sha256"):
                row["result"] = "sha256_mismatch"
            else:
                row["result"] = "ok"
        checks.append(row)

    connector = ROOT / "build" / "connector" / "CONNECTOR_SOURCE.txt"
    clio = ROOT / "src" / "clio-agent"
    metadata = {
        "job_id": args.job,
        "sessions": sessions,
        "commits": {
            "clio-agent": _commit(clio),
            "gact-tui": _commit(clio / "external" / "gact-tui"),
            "clio-agent-marketplace": _commit(clio / "external" / "clio-agent-marketplace"),
            "connector_source": connector.read_text().strip() if connector.is_file() else "",
        },
        "model": {
            "snapshot": str(snapshot),
            "id": "ibm-granite/granite-4.2-8b",
            "revision": snapshot.name.rsplit("--", 1)[-1],
        },
        "journals": journals,
        "decode_attention_tasks": len(tasks),
        "captures_copied": copied,
        "check": {
            "descriptors_for_sessions": len(checks),
            "ok": sum(1 for row in checks if row["result"] == "ok"),
            "not_ok": [row for row in checks if row["result"] != "ok"],
        },
    }
    (out / "metadata.json").write_text(json.dumps(metadata, indent=1))
    total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(
        json.dumps(
            {
                "dir": str(out),
                "bytes": total,
                "check": {k: v if k != "not_ok" else len(v) for k, v in metadata["check"].items()},
            }
        )
    )


if __name__ == "__main__":
    main()
