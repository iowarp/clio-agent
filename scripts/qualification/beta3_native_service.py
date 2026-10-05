"""Qualify the native supervisor with a real pinned Flowcept HTTP service over SSH.

This checks installation/process ownership/reconnect/retention, not provenance
ingest or vLLM inference. No existing service is adopted or modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any

from clio_agent.gact.infrastructure import node_service
from clio_agent.gact.infrastructure.native_vllm import FLOWCEPT_REVISION

LAUNCHER = """import json
import os
from pathlib import Path
root = Path(__file__).parent
settings = {"project": {"dump_buffer": {"enabled": False}}, "log": {"log_file_level": "disable", "log_stream_level": "error"}, "web_server": {"ui_enabled": False, "dashboards_dir": str(root / "evidence/dashboards")}, "mq": {"enabled": False}, "kv_db": {"enabled": False}, "databases": {"mongodb": {"enabled": False}}}
(root / "settings.json").write_text(json.dumps(settings))
os.environ["FLOWCEPT_SETTINGS_PATH"] = str(root / "settings.json")
import uvicorn
uvicorn.run("flowcept.webservice.main:app", host="127.0.0.1", port=18028)
"""


def main() -> None:
    """Use real detached workers, with each status query on a fresh SSH connection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default="homelab")
    parser.add_argument(
        "--root", default="/data/clio-beta3-qualification/native-flowcept-supervisor"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = PurePosixPath(args.root)
    if (
        not root.is_relative_to("/data/clio-beta3-qualification")
        or len(root.parts) < 4
        or ".." in root.parts
    ):
        raise ValueError("Use an owned qualification folder on /data")
    script = Path(node_service.__file__).read_text(encoding="utf-8")
    owner = hashlib.sha256((args.profile + args.root).encode()).hexdigest()
    manifest = {
        "definition_version": "flowcept-native-supervisor-qualification-1",
        "project": '[project]\nname="clio-supervisor-qualification"\nversion="0.0.0"\nrequires-python=">=3.12,<3.13"\ndependencies='
        + json.dumps(
            [
                f"flowcept[webservice,extras] @ git+https://github.com/spotter-ai-genesis/flowcept.git@{FLOWCEPT_REVISION}"
            ]
        )
        + "\n",
        "launcher": LAUNCHER,
        "arguments": [],
        "environment": {},
        "port": 18028,
        "health_path": "/api/v1/health/live",
        "installation_bytes": 512 * 1024**2,
    }

    def remote(program: str, body: dict[str, Any]) -> str:
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", args.profile, shlex.join(["python3", "-c", program])],
            input=json.dumps(body),
            text=True,
            capture_output=True,
            timeout=45,
        )
        if result.returncode:
            raise RuntimeError(result.stderr[-3000:])
        return result.stdout

    def action(verb: str) -> dict[str, Any]:
        output = remote(
            script,
            {
                "action": verb,
                "root": str(root),
                "owner": owner,
                "manifest": manifest,
                "script": script,
                "operation_id": "qualification-" + verb,
            },
        )
        return json.loads(output.removeprefix(node_service.MARKER))

    def await_capability(capability: str) -> dict[str, Any]:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            row = action("status")
            if row[capability]:
                return row
            if row["phase"] in {"failed", "interrupted"}:
                raise RuntimeError(
                    json.dumps(row)
                    + "\n"
                    + remote(script, {"action": "logs", "root": str(root), "owner": owner})
                )
            time.sleep(1)
        raise TimeoutError("Native service did not reach the requested capability")

    try:
        action("prepare")
        first = action("install")
        cancelled = action("stop")
        assert not cancelled["worker_alive"]
        action("install")
        installed = await_capability("installed")
        assert not installed["running"] and not installed["attention_verified"]
        action("start")
        serving = await_capability("serving")
        info = json.loads(
            remote(
                'import json,sys; from urllib.request import urlopen; from pathlib import Path; body=json.load(sys.stdin); value=json.load(urlopen("http://127.0.0.1:18028/api/v1/info")); Path(body["root"],"evidence","http-info.json").write_text(json.dumps(value)); print(json.dumps(value))',
                {"root": str(root)},
            )
        )
        assert info["service"] == "flowcept"
        action("stop")
        stopped = action("status")
        assert stopped["installed"] and not stopped["running"] and not stopped["serving"]
        action("start")
        restarted = await_capability("serving")
        removed = action("uninstall")
        assert not removed["installed"] and not removed["running"]
        retained = json.loads(
            remote(
                'import json,sys; from pathlib import Path; root=Path(json.load(sys.stdin)["root"]); print(json.dumps({"evidence":(root/"evidence/http-info.json").is_file(),"logs":(root/"logs/server.log").is_file(),"cache":(root/"cache").is_dir(),"environment_removed":not(root/"environment").exists()}))',
                {"root": str(root)},
            )
        )
        assert all(retained.values())
        evidence = {
            "host": args.profile,
            "root": str(root),
            "first_install": first,
            "cancelled": cancelled,
            "installed_without_start": installed,
            "serving": serving,
            "http_info": info,
            "stopped": stopped,
            "restarted": restarted,
            "removed": removed,
            "retained": retained,
            "provenance_ingest_tested": False,
            "inference_tested": False,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, indent=2))
    finally:
        action("stop")


if __name__ == "__main__":
    main()
