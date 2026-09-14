"""Run only the release-blocking MCP Apps avenue from the V2 exerciser."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
LEG = ROOT / "scripts" / "live_verification" / "leg_c2_v2_avenues.py"
DEFAULT_OUT = ROOT / "out" / "live-verification" / "release_v0_9_2" / "mcp_apps.json"


def main() -> int:
    """Delegate to the existing live leg with its bounded apps-only selector."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=17982)
    parser.add_argument("--provider", default="codex")
    parser.add_argument("--model", default="gpt-5.6-luna")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(LEG),
        "--apps-ui-only",
        "--port",
        str(args.port),
        "--provider",
        args.provider,
        "--model",
        args.model,
        "--out",
        str(args.out),
    ]
    return subprocess.call(command, cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
