"""Validate and record the exact CLIO v0.9.2 live candidate."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
CAMPAIGN = Path(__file__).resolve().parents[1]
OUT = ROOT / "out" / "live-verification" / "release_v0_9_2"
EXPECTED_UI = "20edc76538d382c866acb3c0cfb29cd8889926ea"
EXPECTED_MARKETPLACE = "067631a44ce40fe1fa90764427a3b8e4e0229852"


def git(*args: str, cwd: Path = ROOT) -> str:
    """Run a read-only Git query and return stripped stdout."""

    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def sha256(path: Path) -> str:
    """Return the SHA-256 digest of one file."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_hashes() -> dict[str, str]:
    """Hash every tracked blueprint snapshot relative to the campaign root."""

    root = CAMPAIGN / "blueprints"
    return {
        path.relative_to(CAMPAIGN).as_posix(): sha256(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def build_manifest() -> dict[str, Any]:
    """Build the exact runtime manifest and reject a dirty or mis-pinned tree."""

    status = git("status", "--porcelain=v1")
    if status:
        raise SystemExit(f"candidate worktree is dirty:\n{status}")

    head = git("rev-parse", "HEAD")
    ui = git("rev-parse", "HEAD", cwd=ROOT / "external" / "gact-tui")
    marketplace = git("rev-parse", "HEAD", cwd=ROOT / "external" / "clio-agent-marketplace")
    if ui != EXPECTED_UI or marketplace != EXPECTED_MARKETPLACE:
        raise SystemExit(
            f"candidate gitlinks are not frozen: gact-tui={ui}, marketplace={marketplace}"
        )

    return {
        "schema_version": 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "candidate": {
            "clio_agent": head,
            "gact_tui": ui,
            "marketplace": marketplace,
        },
        "versions": {
            "clio_agent": "0.9.2",
            "gact_tui": "0.11.0",
            "marketplace": "0.6.3",
        },
        "provider": "codex",
        "model": "gpt-5.6-luna",
        "workspace_ids": [],
        "session_ids": {},
        "gates": {},
        "blueprint_sha256": snapshot_hashes(),
    }


def main() -> int:
    """Write the exact candidate manifest into the untracked evidence root."""

    OUT.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest()
    target = OUT / "manifest.json"
    target.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"candidate={manifest['candidate']['clio_agent']}")
    print(f"manifest={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
