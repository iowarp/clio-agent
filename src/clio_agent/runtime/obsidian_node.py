"""Prepare the official client's Node environment in the installer's own process."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from clio_agent.runtime.document_runtime import prepare_document_runtime


def main() -> None:
    """Reuse the locked execution environment under the caller's scoped installer policy."""
    parser = argparse.ArgumentParser()
    parser.add_argument("workspace", type=Path)
    args = parser.parse_args()
    runtime = prepare_document_runtime(args.workspace, discovery_only=True)
    print(json.dumps({"node": runtime["node"]}))


if __name__ == "__main__":
    main()
