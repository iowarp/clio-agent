"""Start one synthetic server over stdio: ``python run.py <server-name>``.

Environment: ``EVAL_SEED`` (default 7), ``EVAL_RECORDS_COUNT`` (``records``
only, default 20), ``EVAL_CALL_LOG`` (see ``toolkit.call_log_path``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a file from a plugin folder
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import distractors  # type: ignore[import-not-found]
    import home  # type: ignore[import-not-found]
    import records  # type: ignore[import-not-found]
    from toolkit import CallLog, call_log_path  # type: ignore[import-not-found]
else:
    from . import distractors, home, records
    from .toolkit import CallLog, call_log_path

SERVERS = ("synthetic-home", "synthetic-calendar", "synthetic-inventory", "records")


def build(name: str, env: dict[str, str] | None = None) -> Any:
    """The named server, configured from ``env`` (default: the process environment)."""
    source = dict(os.environ if env is None else env)
    seed = int(source.get("EVAL_SEED") or 7)
    log = CallLog(name, call_log_path(source))
    if name == "synthetic-home":
        return home.build(seed, log)
    if name == "synthetic-calendar":
        return distractors.build_calendar(seed, log)
    if name == "synthetic-inventory":
        return distractors.build_inventory(seed, log)
    if name == "records":
        return records.build(int(source.get("EVAL_RECORDS_COUNT") or 20), seed, log)
    raise SystemExit(f"unknown server {name!r}; expected one of {', '.join(SERVERS)}")


def main(argv: list[str]) -> None:
    if len(argv) != 2:
        raise SystemExit(f"usage: run.py <{'|'.join(SERVERS)}>")
    build(argv[1]).run(show_banner=False)


if __name__ == "__main__":
    main(sys.argv)
