"""Run a real, isolated CLIO API for provenance UI checks without constructing an inference agent."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def serve(root: Path, port: int) -> None:
    """Keep test configuration and session state under the explicitly selected directory."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    os.chdir(root)
    os.environ.pop("CLIO_USER_DIR", None)
    os.environ.update(
        CLIO_AGENT_HOME=str(root / "agent-home"),
        CLIO_CORE_HOME=str(root / "core-home"),
        CLIO_SHARED_HOME=str(root / "shared-home"),
        CLIO_ALLOWED_ROOTS=str(root),
        CLIO_GACT_CORS_ORIGINS="http://127.0.0.1:4394",
        CLIO_ENV_FILE_LOADED="1",
    )
    import uvicorn

    from clio_agent.gact.app import build_app

    app = build_app(agent=None, sessions_path=root / "agent-home/sessions.json")
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18825)
    args = parser.parse_args()
    serve(args.state_dir, args.port)
