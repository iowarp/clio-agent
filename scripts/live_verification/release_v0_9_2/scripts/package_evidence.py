"""Package the completed v0.9.2 live evidence without changing the candidate."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / "out" / "live-verification" / "release_v0_9_2"


def main() -> int:
    """Create a zip beside the evidence directory after validating required files."""

    required = [OUT / "manifest.json", OUT / "verdict.md"]
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        raise SystemExit(f"cannot package incomplete evidence: {', '.join(missing)}")
    archive = shutil.make_archive(
        str(OUT.parent / "clio-agent-v0.9.2-live-verification"), "zip", OUT
    )
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
