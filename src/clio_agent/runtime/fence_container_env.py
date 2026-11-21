"""In-fence environment for a Windows MXC child (stdlib only, run by path).

Codex's native MXC container replaces ``LOCALAPPDATA``, ``TEMP`` and ``TMP`` with a
per-launch ``Packages\\sandbox.{GUID}\\AC`` profile that the fence does not grant and that
is discarded on the next launch, so ``uv`` and other tools fail with *Access is denied*
(os error 5). Setting them before the container starts does not help: MXC derives its
profile from the incoming ``LOCALAPPDATA`` and the bootstrap then breaks (errors 3 and 267).

CLIO therefore starts the child as ``python -I -S -B fence_container_env.py <json> <line>``
inside the container. This sets the variables in ``<json>`` and runs ``<line>``, the
child's command line quoted the way ``subprocess`` would quote it unfenced. Stdio is
inherited, and the child's exit code is passed back.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def _inherited(fd: int) -> int | None:
    try:
        os.fstat(fd)
    except OSError:
        return None
    return fd


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fence_container_env.py <json env> <command line>", file=sys.stderr)
        return 2
    environment = json.loads(argv[0])
    os.environ.update({str(key): str(value) for key, value in environment.items()})
    for folder in {os.environ.get("TEMP", ""), os.environ.get("TMP", "")} - {""}:
        os.makedirs(folder, exist_ok=True)
    child = subprocess.Popen(
        argv[1], stdin=_inherited(0), stdout=_inherited(1), stderr=_inherited(2)
    )
    while True:
        try:
            return child.wait()
        except KeyboardInterrupt:
            continue


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
