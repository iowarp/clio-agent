"""Install locked execution packages and a private Office renderer after app installation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from clio_agent import paths
from clio_agent.runtime.document_runtime import (
    DocumentRuntimeError,
    prepare_document_runtime,
    prepare_office_runtime,
)
from clio_agent.runtime.github_cli import ensure_github_cli


def install_document_runtime(workspace: Path, *, cache_root: Path | None = None) -> dict[str, Any]:
    """Provision and verify all required packages without enlarging the installer payload."""
    workspace.mkdir(parents=True, exist_ok=True)
    result = prepare_document_runtime(workspace, cache_root=cache_root)
    if result["javascript"]["status"] != "ready":
        raise DocumentRuntimeError(f"Node/pnpm package installation failed: {result['javascript']}")
    office = prepare_office_runtime()
    result["native_tools"]["soffice"] = {"status": "available", "path": office}
    result["capabilities"]["office_render_recalculate"] = "available"
    github = ensure_github_cli()
    result["native_tools"]["gh"] = {
        "status": "available",
        "path": str(github),
        "authentication": "clio_account",
    }
    receipt = (
        (cache_root or paths.user_cache_dir() / "document-runtime")
        / result["runtime_id"]
        / "installed.json"
    )
    receipt.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def main() -> None:
    """Run the shared installer step; an incomplete setup exits unsuccessfully."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=paths.user_data_dir() / "workspace")
    args = parser.parse_args()
    # This standalone installer owns its workspace. Never widen a running agent's policy.
    os.environ["CLIO_ALLOWED_ROOTS"] = str(args.workspace.resolve())
    print("Installing locked Python/uv and Node/pnpm packages and Office rendering...", flush=True)
    result = install_document_runtime(args.workspace)
    print(
        json.dumps(
            {
                "status": result["status"],
                "runtime_id": result["runtime_id"],
                "office": result["native_tools"]["soffice"],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
