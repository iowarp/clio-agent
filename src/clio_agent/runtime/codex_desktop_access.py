"""Reuse Desktop runtime grants only while their directory identity and ACL agree."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from filelock import FileLock

from clio_agent import paths
from clio_agent.runtime.sandbox_cli import (
    FleetGrant,
    _run_icacls,
    build_fleet_runtime_grant_plan,
    grant_fleet_runtime_access,
)


def _identity(grant: FleetGrant) -> dict[str, Any] | None:
    """Check one root, without walking any of its descendants."""
    try:
        stat = Path(grant.path).stat()
        code, acl = _run_icacls(["icacls", grant.path])
        if code != 0:
            return None
        return {
            "path": grant.path,
            "users": list(grant.users),
            "inherit": grant.inherit,
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "created": stat.st_ctime_ns,
            "acl": hashlib.sha256(acl.encode()).hexdigest(),
        }
    except OSError:
        return None


def ensure_desktop_runtime_access(
    *, progress: Callable[[str], None] | None = None
) -> list[dict[str, Any]]:
    """Reuse successful grants for unchanged roots; repair changed or unknown roots.

    This receipt covers read/execute access only. The separate sandbox gate must
    still establish account presence and verified write confinement. Explicit
    ``clio sandbox setup`` retains its unconditional repair behavior.
    """
    receipt = paths.user_config_dir() / "sandbox" / "desktop-runtime-access.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    reasons: list[dict[str, Any]] = []
    with FileLock(str(receipt.with_suffix(".lock")), timeout=30):
        try:
            previous = json.loads(receipt.read_text(encoding="utf-8"))
            saved = previous.get("grants", {}) if previous.get("schema") == 1 else {}
            if not isinstance(saved, dict):
                saved = {}
        except (OSError, ValueError, AttributeError):
            saved = {}
        current: dict[str, Any] = {}
        for grant in build_fleet_runtime_grant_plan():
            if grant.label == "clio_kit_cache" and not grant.exists:
                # Give future tool packages inherited access without granting
                # anything on the neighboring private agent state/config.
                Path(grant.path).mkdir(parents=True, exist_ok=True)
                grant = replace(grant, exists=True)
            key = grant.label
            label = {
                "bundled_runtime": "the local runtime",
                "clio_kit_cache": "tool packages",
                "user_temp": "temporary workspaces",
            }.get(key, key.replace("_", " "))
            identity = _identity(grant) if grant.exists else None
            record = saved.get(key)
            if (
                identity is not None
                and isinstance(record, dict)
                and record.get("identity") == identity
            ):
                cached = record.get("reasons")
                if (
                    isinstance(cached, list)
                    and len(cached) == len(grant.users)
                    and all(
                        isinstance(item, dict) and item.get("status") == "granted"
                        for item in cached
                    )
                ):
                    reasons.extend({**item, "cached": True} for item in cached)
                    current[key] = record
                    if progress:
                        progress(f"Access to {label} is ready.")
                    continue
            if progress:
                progress(f"Preparing access to {label}...")
            applied = grant_fleet_runtime_access(plan=[grant], platform="win32")
            reasons.extend(applied)
            identity = _identity(grant) if grant.exists else None
            if (
                identity is not None
                and applied
                and all(item["status"] == "granted" for item in applied)
            ):
                current[key] = {"identity": identity, "reasons": applied}
            # Commit completed roots individually: a later failure must neither
            # repeat successful scans nor certify an unfinished grant.
            temporary = receipt.with_suffix(".tmp")
            temporary.write_text(json.dumps({"schema": 1, "grants": current}), encoding="utf-8")
            temporary.replace(receipt)
    return reasons
