#!/usr/bin/env python3
"""List active ALCF inference models via the Argonne /jobs endpoint."""

from __future__ import annotations

import argparse
import json
from typing import Any

import requests

from clio_agent.providers.argonne_auth import get_access_token


def list_active_models(cluster: str) -> list[dict[str, str]]:
    """Return active models for one ALCF inference cluster."""
    token = get_access_token()
    response = requests.get(
        f"https://inference-api.alcf.anl.gov/resource_server/{cluster}/jobs",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    response.raise_for_status()
    payload: dict[str, Any] = response.json()

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for job in payload.get("running") or []:
        for raw_model in str(job.get("Models") or "").split(","):
            model = raw_model.strip()
            if not model or model in seen:
                continue
            seen.add(model)
            rows.append(
                {
                    "cluster": cluster,
                    "model": model,
                    "framework": str(job.get("Framework") or ""),
                    "job_id": str(job.get("Job ID") or ""),
                    "nodes": str(job.get("Nodes Reserved") or ""),
                    "walltime": str(job.get("Walltime") or ""),
                }
            )
    return rows


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="List active ALCF inference models")
    parser.add_argument(
        "--cluster",
        action="append",
        default=[],
        help="Cluster to query. Repeatable. Defaults to sophia and metis.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    args = parser.parse_args()

    clusters = args.cluster or ["sophia", "metis"]
    rows: list[dict[str, str]] = []
    for cluster in clusters:
        rows.extend(list_active_models(cluster))

    if args.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return

    if not rows:
        print("No active ALCF models found.")
        return

    for row in rows:
        suffix = []
        if row["framework"]:
            suffix.append(f"framework={row['framework']}")
        if row["walltime"]:
            suffix.append(f"walltime={row['walltime']}")
        detail = f" ({', '.join(suffix)})" if suffix else ""
        print(f"{row['cluster']}: {row['model']}{detail}")


if __name__ == "__main__":
    main()
