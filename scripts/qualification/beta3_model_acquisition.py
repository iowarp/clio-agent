"""Run real target-side cancellation, immutable retry, verification and cache reuse."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from pathlib import Path

from clio_agent.gact.infrastructure import node_models

RUNNER = """
import json, os, re, sys, time
from pathlib import Path
request = json.load(sys.stdin)
namespace = {"__name__": "clio_model_qualification"}
exec(compile(request["script"], "node_models.py", "exec"), namespace)
root = Path(request["root"]) / "model-operations" / re.sub(r"[^A-Za-z0-9_.-]", "_", os.uname().nodename)
first = namespace["start"](root, request, request["script"])
cancelled = namespace["cancel"](root, first["id"])
assert cancelled["state"] == "cancelled", cancelled
second = namespace["start"](root, request, request["script"])
assert second["id"] == first["id"]
deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    rows = namespace["inspect_jobs"](root)
    row = next(row for row in rows if row["id"] == first["id"])
    if row["state"] not in {"queued", "running"}:
        break
    time.sleep(0.5)
assert row["state"] == "ready", row
before = {str(path): path.stat().st_mtime_ns for path in Path(row["destination"]).rglob("*") if path.is_file()}
reused = namespace["start"](root, request, request["script"])
after = {str(path): path.stat().st_mtime_ns for path in Path(row["destination"]).rglob("*") if path.is_file()}
assert reused["state"] == "ready" and before == after
print(json.dumps({"hostname": os.uname().nodename, "cancelled_before_retry": cancelled["state"], "cache_reused_without_file_changes": True, "receipt": reused}))
"""


def main() -> None:
    """Retain sanitized evidence from a bounded real model download; never run inference."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.root.startswith(
        ("/data/clio-beta3-qualification/", "/mnt/common/clio-beta3-qualification/")
    ):
        raise ValueError("Use a dedicated beta-3 qualification folder")
    script = Path(node_models.__file__).read_text(encoding="utf-8")
    payload = {
        "script": script,
        "root": args.root,
        "repository": "sshleifer/tiny-gpt2",
        "revision": "5f91d94bd9cd7190a9f3216ff93cd1dd95f2c7be",
        "destination": args.root + "/models/tiny-gpt2",
    }
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", args.profile, shlex.join(["python3", "-c", RUNNER])],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=240,
        check=True,
    )
    evidence = json.loads(result.stdout)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
