"""Installed monitoring launcher: one collector, owned dependencies, loopback API."""

from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path
from typing import Any


def main() -> None:
    """Serve Flowcept or supervise CMF independently from model inference."""
    from stack import (  # type: ignore[import-not-found]
        cleanup,
        flowcept_environment,
        running,
        start,
    )

    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "manifest.json").read_text())
    stopped = False

    def stop(signum: int, frame: Any) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    try:
        start(root, manifest)
        if manifest["service"] == "flowcept":
            flowcept_environment(root)
            import uvicorn
            from flowcept.flowceptor.consumers.document_inserter import DocumentInserter
            from flowcept.webservice.main import app

            owner = json.loads((root / "owner.json").read_text())["owner"]
            collector = DocumentInserter(check_safe_stops=False, bundle_exec_id=owner)
            collector.start(threaded=True)
            try:
                uvicorn.run(app, host="127.0.0.1", port=manifest["port"], log_level="warning")
            finally:
                collector.stop(bundle_exec_id=owner)
        else:
            while not stopped:
                for component in manifest["components"]:
                    if not running(root, manifest, component):
                        raise RuntimeError(f"Owned {component['role']} container stopped")
                time.sleep(1)
    finally:
        cleanup(root, manifest, remove=False)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
