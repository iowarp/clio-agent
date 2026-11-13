"""Record the files a CLIO server start opens, for the installer's prewarm.

Run it with an installed CLIO environment's interpreter, against a scratch home:

    .venv\\Scripts\\python.exe scripts/record_server_imports.py --home D:\\scratch\\h --port 18790

It starts ``clio-agent serve`` in that home, waits until ``/v1/health`` reports ready,
then has the server list every imported module's file and every native library it has
mapped. The paths are written relative to the import-path folder holding them to
``src/clio_agent/runtime/server_imports.txt``, which ``document_install`` opens once
during an install so an on-access scanner has seen them before the first start. The
server and the processes it started are stopped afterwards.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import psutil

OUT = Path(__file__).resolve().parents[1] / "src" / "clio_agent" / "runtime" / "server_imports.txt"

_BOOT = r"""
import json, os, sys, threading, time
port, raw = sys.argv[1], sys.argv[2]
def _dump():
    while not os.path.exists(raw + ".request"):
        time.sleep(0.2)
    modules = sorted({getattr(m, "__file__", None) or "" for m in list(sys.modules.values())})
    try:
        import psutil
        maps = [m.path for m in psutil.Process().memory_maps()]
    except Exception:
        maps = []
    with open(raw + ".tmp", "w", encoding="utf-8") as handle:
        json.dump({"files": [f for f in modules if f] + maps, "path": sys.path}, handle)
    os.replace(raw + ".tmp", raw)
threading.Thread(target=_dump, daemon=True).start()
sys.argv = ["clio-agent", "serve", "--port", port]
from clio_agent.ui.cli import main
main()
"""


def _ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/health", timeout=3) as reply:
            return json.load(reply).get("readiness") == "ready"
    except (OSError, ValueError):
        return False


def _relative(files: list[str], import_path: list[str]) -> list[str]:
    folders = sorted(
        {os.path.normcase(os.path.realpath(p)) for p in import_path if p and os.path.isdir(p)},
        key=len,
        reverse=True,
    )
    entries: set[str] = set()
    for name in files:
        real = os.path.normcase(os.path.realpath(name))
        if not os.path.isfile(real):
            continue
        for folder in folders:
            if real.startswith(folder.rstrip(os.sep) + os.sep):
                rel = os.path.relpath(real, folder).replace(os.sep, "/")
                if "__pycache__/" not in rel:
                    entries.add(rel)
                break
    return sorted(entries)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", required=True, help="scratch CLIO_AGENT_HOME")
    parser.add_argument("--port", type=int, default=18790)
    parser.add_argument("--settle", type=float, default=10.0, help="seconds after ready")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()

    home = Path(args.home).resolve()
    home.mkdir(parents=True, exist_ok=True)
    raw = home / "server-imports.json"
    for stale in (raw, Path(f"{raw}.request")):
        stale.unlink(missing_ok=True)
    env = {**os.environ, "CLIO_AGENT_HOME": str(home)}
    env.pop("CLIO_PORT", None)
    server = subprocess.Popen(
        [sys.executable, "-c", _BOOT, str(args.port), str(raw)],
        cwd=home,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    owned = psutil.Process(server.pid)
    try:
        deadline = time.monotonic() + args.timeout
        while not _ready(args.port):
            if server.poll() is not None or time.monotonic() > deadline:
                print("the server did not become ready", file=sys.stderr)
                return 1
            time.sleep(1)
        time.sleep(args.settle)
        Path(f"{raw}.request").touch()
        while not raw.exists():
            if time.monotonic() > deadline:
                print("the server did not list its imports", file=sys.stderr)
                return 1
            time.sleep(0.2)
        dump = json.loads(raw.read_text(encoding="utf-8"))
    finally:
        tree = [owned, *owned.children(recursive=True)]
        for proc in reversed(tree):
            try:
                proc.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(tree, timeout=15)
        # Supervised services (web search) leave the server's tree; stop them as `clio stop` does.
        subprocess.run(
            [sys.executable, "-I", "-m", "clio_agent.gact.infrastructure.local_services", "stop"],
            cwd=home,
            env=env,
            check=False,
            capture_output=True,
        )
    entries = _relative(dump["files"], dump["path"])
    header = (
        "# Files a CLIO server start opens, relative to their import-path folder.\n"
        "# Written by scripts/record_server_imports.py; read by runtime/document_install.\n"
    )
    args.out.write_text(header + "\n".join(entries) + "\n", encoding="utf-8")
    print(f"{len(entries)} entries -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
