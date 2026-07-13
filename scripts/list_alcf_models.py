#!/usr/bin/env python3
"""List ALCF inference models via Argonne's endpoint catalog."""

from __future__ import annotations

import argparse
import json
from typing import Any

import requests

from clio_agent.providers.argonne_auth import get_access_token

CATALOG_URL = "https://inference-api.alcf.anl.gov/resource_server/list-endpoints"


def _framework_for_cluster(cluster: str) -> str:
    """Return the default OpenAI-compatible framework for an ALCF cluster."""
    return "api" if cluster == "metis" else "vllm"


def _jobs_by_model(cluster: str, token: str, timeout: float) -> dict[str, dict[str, str]]:
    """Return optional live job metadata keyed by model id.

    The ``/jobs`` endpoint is useful but can be slow or unavailable. It should
    annotate model rows, not decide whether the catalog itself works.
    """
    try:
        response = requests.get(
            f"https://inference-api.alcf.anl.gov/resource_server/{cluster}/jobs",
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
    except Exception:
        return {}

    rows: dict[str, dict[str, str]] = {}
    for job in payload.get("running") or []:
        for raw_model in str(job.get("Models") or "").split(","):
            model = raw_model.strip()
            if not model or model in rows:
                continue
            rows[model] = {
                "framework": str(job.get("Framework") or ""),
                "job_id": str(job.get("Job ID") or ""),
                "nodes": str(job.get("Nodes Reserved") or ""),
                "walltime": str(job.get("Walltime") or ""),
                "status": "running",
            }
    return rows


def list_models(cluster: str, *, timeout: float = 30.0) -> list[dict[str, str]]:
    """Return catalog models for one ALCF inference cluster."""
    token = get_access_token()
    catalog = requests.get(
        CATALOG_URL,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    catalog.raise_for_status()
    payload: dict[str, Any] = catalog.json()

    framework = _framework_for_cluster(cluster)
    models = (payload.get("clusters") or {}).get(cluster, {}).get("frameworks", {}).get(
        framework, {}
    ).get("models") or []
    jobs = _jobs_by_model(cluster, token, min(timeout, 12.0))
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_model in models:
        model = str(raw_model).strip()
        if not model or model in seen:
            continue
        seen.add(model)
        live = jobs.get(model, {})
        rows.append(
            {
                "cluster": cluster,
                "model": model,
                "framework": live.get("framework") or framework,
                "job_id": live.get("job_id", ""),
                "nodes": live.get("nodes", ""),
                "walltime": live.get("walltime", ""),
                "status": live.get("status", "catalog"),
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
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds for catalog requests.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    args = parser.parse_args()

    clusters = args.cluster or ["sophia", "metis"]
    rows: list[dict[str, str]] = []
    for cluster in clusters:
        rows.extend(list_models(cluster, timeout=args.timeout))

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
        if row["status"]:
            suffix.append(f"status={row['status']}")
        detail = f" ({', '.join(suffix)})" if suffix else ""
        print(f"{row['cluster']}: {row['model']}{detail}")


if __name__ == "__main__":
    main()
