#!/usr/bin/env python3
"""Per-file ratchet on broad excepts hidden behind ``noqa`` (clio-core fail-stop).

``check_silent_fallbacks.py`` counts ``BLE001`` / ``S110`` / ``E722`` with ``noqa``
honoured, so it read 0 while 583 broad excepts sat behind ``# noqa: BLE001`` (audit,
2026-09-30). This guard counts them with ``noqa`` ignored, per file under
``src/clio_agent``, against :data:`BASELINE_PATH`. A file above its recorded count -- or
a new file with any -- fails, so swallows can only be burned down, never moved or added.
``--lower-baseline`` writes the lower of the recorded and live counts (never raises one).

    uv run python scripts/check_noqa_swallows.py
    uv run python scripts/check_noqa_swallows.py --lower-baseline
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

RULES = ("BLE001", "S110", "E722")
SRC_ROOT = "src/clio_agent"
BASELINE_PATH = Path(__file__).resolve().parent / "silent_fallback_noqa_baseline.json"


def count_hidden_by_file(path: Path) -> dict[str, int]:
    """``{relative posix path: count}`` of the ratcheted rules under ``path``, noqa ignored.

    Raises:
        RuntimeError: if ruff fails or its JSON output does not parse.
    """
    env = {k: v for k, v in os.environ.items() if k not in {"FORCE_COLOR", "CLICOLOR_FORCE"}}
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            ",".join(RULES),
            "--isolated",
            "--ignore-noqa",
            "--output-format",
            "json",
            "--quiet",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**env, "NO_COLOR": "1"},
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"ruff failed (exit {proc.returncode}): {proc.stderr.strip()}")
    try:
        diagnostics = json.loads(proc.stdout or "[]")
    except ValueError as exc:
        raise RuntimeError(f"ruff JSON output did not parse: {proc.stdout[:500]!r}") from exc
    root = path.resolve()
    return dict(
        Counter(
            Path(d["filename"]).resolve().relative_to(root).as_posix()
            for d in diagnostics
            if d.get("code") in RULES
        )
    )


def main(argv: list[str] | None = None) -> int:
    """Return 0 when no file exceeds its baseline, 1 otherwise."""
    parser = argparse.ArgumentParser(description=__doc__)
    repo = Path(__file__).resolve().parent.parent
    parser.add_argument("--path", type=Path, default=repo / SRC_ROOT)
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    parser.add_argument("--lower-baseline", action="store_true")
    args = parser.parse_args(argv)

    live = count_hidden_by_file(args.path)
    recorded: dict[str, int] = (
        json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline.is_file() else {}
    )
    over = sorted((f, n) for f, n in live.items() if n > recorded.get(f, 0))
    under = sorted(f for f, n in recorded.items() if live.get(f, 0) < n)
    print(f"noqa-hidden swallows over {args.path}: {sum(live.values())} in {len(live)} files")
    for name, count in over:
        print(f"FAIL {name}: {count} > {recorded.get(name, 0)}")
    if args.lower_baseline:
        lowered = {f: min(n, live.get(f, 0)) for f, n in recorded.items()} if recorded else live
        lowered = {f: n for f, n in sorted(lowered.items()) if n > 0}
        args.baseline.write_text(json.dumps(lowered, indent=1) + "\n", encoding="utf-8")
        print(f"baseline written: {sum(lowered.values())} in {len(lowered)} files")
    elif under:
        print(f"OK: {len(under)} file(s) dropped below the baseline; run --lower-baseline.")
    if over:
        print("Fix the new swallow (typed failure, or a sanctioned loud fallback) instead.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
