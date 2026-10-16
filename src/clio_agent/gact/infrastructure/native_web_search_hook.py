"""Installed native gateway lifecycle; its child SearXNG shares the owned process group."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import ProxyHandler, build_opener


def get(url: str) -> Any:
    """Read a local JSON endpoint without inheriting an outgoing proxy."""
    with build_opener(ProxyHandler({})).open(url, timeout=60) as response:
        return json.load(response)


def serve(root: Path, manifest: dict[str, Any]) -> None:
    """Run both real services and stop the gateway if its search backend exits."""
    import uvicorn  # noqa: PLC0415
    from clio_web_search.config import Settings  # type: ignore[import-not-found]  # noqa: PLC0415
    from clio_web_search.main import create_app  # type: ignore[import-not-found]  # noqa: PLC0415

    private = manifest["searxng"]["settings"]["server"]["port"]
    gateway = manifest["gateway"]
    settings = Settings(
        host="127.0.0.1",
        port=manifest["port"],
        data_dir=root / "data",
        searxng_url=f"http://127.0.0.1:{private}",
        grobid_url=gateway["grobid_url"] or "http://127.0.0.1:18070",
        documents_enabled=gateway["documents"],
        contact_email=gateway["contact_email"] or None,
    )
    with subprocess.Popen([sys.executable, str(root / "searxng_launch.py")], cwd=root) as search:
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(settings), host="127.0.0.1", port=manifest["port"], log_level="warning"
            )
        )

        def watch() -> None:
            search.wait()
            server.should_exit = True

        threading.Thread(target=watch, daemon=True).start()
        try:
            server.run()
            if search.poll() is not None:
                raise RuntimeError("Private SearXNG exited; inspect server logs")
        finally:
            if search.poll() is None:
                search.terminate()
            try:
                search.wait(timeout=15)
            except subprocess.TimeoutExpired:
                search.kill()
                search.wait()


def main() -> None:
    """Install, serve, inspect or perform a fresh real search through the gateway."""
    import searxng_hook  # type: ignore[import-not-found]  # noqa: PLC0415

    root = Path(__file__).resolve().parent
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    action = sys.argv[1] if len(sys.argv) > 1 else "verify"
    base = f"http://127.0.0.1:{manifest['port']}"
    if action == "install":
        searxng_hook.install(root)
    elif action == "serve":
        serve(root, manifest)
    elif action == "running":
        if get(base + "/healthz").get("version") != "0.3.2" or not searxng_hook.running(root):
            raise RuntimeError("Native gateway or its private SearXNG identity is unavailable")
    elif action == "verify":
        result = get(
            base + "/search?" + urlencode({"q": "HDF5 hierarchical data format", "format": "json"})
        )
        rows = [row for row in result.get("results", []) if row.get("url")]
        if not rows:
            raise RuntimeError("Native Web Search returned no results; inspect engine failures")
        print(
            json.dumps(
                {
                    "service": "web_search",
                    "search_ok": True,
                    "result_count": len(rows),
                    "first_result_url": rows[0]["url"],
                    "verified_at": time.time(),
                }
            )
        )
    else:
        raise ValueError("Unsupported native Web Search lifecycle action")


if __name__ == "__main__":
    main()
