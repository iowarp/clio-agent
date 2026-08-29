"""Owner module for the persisted agent-blueprint SOURCE registry.

The source registry is the per-user ledger of marketplace/source registrations
(``agent-blueprint-sources.json``). Carved out of ``routes/blueprints.py``
(#775 no-accretion — the routes file is ratchet-baselined) when the registry
gained self-healing:

* **Dead-fixture pruning** — ~100 rows pointing at long-deleted pytest temp
  dirs accumulated because the tests' XDG-based isolation is a no-op on
  Windows (platformdirs ignores ``XDG_CONFIG_HOME`` there), so every test run
  registered its tmpdir fixture into the REAL registry. Pruning is
  deliberately narrow: only local paths that are gone AND look like temp/test
  residue are dropped — a source on an unmounted network drive or an SCP-style
  remote (``host:path``) must never be deleted by a read (review 2026-08-13).
* **Atomic persistence** — the ledger is written via tmp + ``os.replace`` so a
  crash mid-write can never truncate it to zero sources.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import subprocess
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Mapping

logger = logging.getLogger(__name__)
_SOURCE_REGISTRY_LOCK = threading.Lock()


def sources_path() -> Path:
    """Return the on-disk path of the blueprint-source registry JSON."""

    from clio_agent import paths  # noqa: PLC0415

    return paths.user_config_dir() / "agent-blueprint-sources.json"


def source_registry_id(source: str, ref: str = "") -> str:
    """Derive a stable ``src_*`` id from a source URL/path and optional ref."""

    digest = hashlib.sha256(f"{source}\n{ref}".encode("utf-8")).hexdigest()[:12]
    return f"src_{digest}"


def refresh_agent_blueprint_source(row: Mapping[str, Any]) -> dict[str, Any]:
    """Inspect a source, cloning remotes temporarily, and list its blueprints."""

    from clio_agent.gact.routes.blueprint_candidates import (  # noqa: PLC0415
        agent_blueprint_candidates,
    )

    source = str(row.get("source") or "").strip()
    ref = str(row.get("ref") or "").strip()
    refreshed = {**dict(row), "ref": ref, "available_blueprints": []}
    if not source:
        return {**refreshed, "status": "error", "error": "source is empty"}
    source_path = Path(source).expanduser()
    refreshed.update(
        source_kind="path" if source_path.exists() else "git",
        status="ready",
        error="",
    )
    try:
        if source_path.exists():
            try:
                refreshed["commit"] = subprocess.check_output(
                    ["git", "-C", str(source_path), "rev-parse", "HEAD"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
            except Exception:  # noqa: BLE001 - commit is optional display metadata
                refreshed["commit"] = ""
            refreshed["available_blueprints"] = agent_blueprint_candidates(source_path)
            return refreshed
        with tempfile.TemporaryDirectory(prefix="clio-agent-blueprint-source-") as tmp:
            clone_target = Path(tmp) / "repo"
            command = ["git", "clone", "--depth", "1"]
            if ref:
                command.extend(["--branch", ref])
            command.extend([source, str(clone_target)])
            subprocess.run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
                env={
                    **os.environ,
                    "GIT_TERMINAL_PROMPT": "0",
                    "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
                },
            )
            refreshed["commit"] = subprocess.check_output(
                ["git", "-C", str(clone_target), "rev-parse", "HEAD"], text=True
            ).strip()
            refreshed["available_blueprints"] = agent_blueprint_candidates(clone_target)
            return refreshed
    except Exception as exc:  # noqa: BLE001 - source diagnostics belong on the source row
        refreshed.update(status="error", error=str(exc))
        return refreshed


def _is_prunable_dead_fixture(source: str) -> bool:
    """Whether a source row is dead LOCAL TEST RESIDUE (and only that).

    Three gates, all required: it parses as a local path (no URL scheme, no
    SCP-style ``host:path`` remote), the path does not exist, and it sits under
    a temp root (``pytest-of`` marker or the OS temp dir). Anything else —
    unreachable network drives, relative paths, remotes — is kept: gone-forever
    versus unreachable-right-now cannot be distinguished on a read.
    """

    if not source or "://" in source or source.startswith("git@"):
        return False
    # SCP-style remotes (host:path) have a colon NOT followed by a path
    # separator in position 2+ (Windows drive letters are `X:\` / `X:/`).
    colon = source.find(":")
    if colon > 1 or (colon == 1 and (len(source) < 3 or source[2] not in "\\/")):
        if colon != 1:
            return False
    path = Path(source).expanduser()
    if not path.is_absolute() or path.exists():
        return False
    lowered = source.lower().replace("\\", "/")
    temp_root = tempfile.gettempdir().lower().replace("\\", "/")
    return "pytest-of" in lowered or lowered.startswith(temp_root)


def load_agent_blueprint_sources() -> list[dict[str, Any]]:
    """Load the persisted blueprint-source rows (empty list if absent/corrupt).

    Self-heals dead test-fixture rows (see :func:`_is_prunable_dead_fixture`);
    the pruned ledger is rewritten atomically with a logged count.
    """

    path = sources_path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - unreadable/invalid sources file yields no rows
        return []
    rows = payload.get("sources") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    loaded = [dict(row) for row in rows if isinstance(row, dict)]
    kept = [row for row in loaded if not _is_prunable_dead_fixture(str(row.get("source") or ""))]
    pruned = len(loaded) - len(kept)
    if pruned:
        logger.warning(
            "blueprint_sources_pruned reason=dead_test_fixture count=%d file=%s",
            pruned,
            path,
        )
        save_agent_blueprint_sources(kept)
    return kept


def save_agent_blueprint_sources(rows: list[dict[str, Any]]) -> None:
    """Persist the blueprint-source rows atomically (tmp + ``os.replace``)."""

    path = sources_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"sources": rows}, indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def record_default_agent_blueprint_source(
    *,
    source: str,
    ref: str,
    pinned_commit: str,
    install_root: Path,
) -> dict[str, Any]:
    """Persist the bundled marketplace alongside its installed blueprints.

    The default marketplace is installed automatically, so it must also be
    visible through the same source ledger as user-added marketplaces. Build
    the catalog from the installed, source-matching snapshots instead of
    cloning the remote a second time during first-run bootstrap.
    """

    from clio_agent.gact.agent_blueprints import read_install_metadata  # noqa: PLC0415
    from clio_agent.gact.routes.blueprint_candidates import (  # noqa: PLC0415
        agent_blueprint_candidates,
    )

    source = str(source).strip()
    now = datetime.now(UTC).isoformat()
    available: list[dict[str, Any]] = []
    commit = ""
    source_kind = "path" if Path(source).expanduser().exists() else "git"
    for candidate in sorted(install_root.iterdir()) if install_root.is_dir() else ():
        if not candidate.is_dir() or not candidate.joinpath("AGENT.md").exists():
            continue
        metadata = read_install_metadata(candidate)
        if str(metadata.get("source") or "").strip() != source:
            continue
        available.extend(agent_blueprint_candidates(candidate))
        commit = commit or str(metadata.get("commit") or "").strip()
        source_kind = str(metadata.get("source_kind") or source_kind).strip() or source_kind

    source_id = source_registry_id(source, ref)
    with _SOURCE_REGISTRY_LOCK:
        rows = load_agent_blueprint_sources()
        existing = next((row for row in rows if row.get("id") == source_id), {})
        row = {
            **existing,
            "id": source_id,
            "name": "CLIO Agent Marketplace",
            "source": source,
            "ref": ref,
            "commit": commit,
            "pinned_commit": pinned_commit,
            "source_kind": source_kind,
            "status": "ready" if available else "degraded",
            "error": "" if available else "installed marketplace exposed no blueprints",
            "added_at": str(existing.get("added_at") or now),
            "updated_at": now,
            "available_blueprints": available,
            "install_scope": "global",
            "is_default": True,
        }
        save_agent_blueprint_sources(
            [existing_row for existing_row in rows if existing_row.get("id") != source_id] + [row]
        )
    return row


def install_agent_blueprint_source(
    row: Mapping[str, Any],
    *,
    cwd: Path,
    scope: Literal["global", "workspace"] = "global",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Install every valid blueprint exposed by a refreshed marketplace source.

    Marketplace registration is an installation operation, not merely catalog
    discovery.  Invalid entries remain visible in the returned ``skipped`` list
    and degrade the source row; an installation failure marks the row as an
    error.  Callers can therefore never advertise a ready marketplace whose
    blueprints are unusable.
    """

    refreshed = dict(row)
    refreshed["install_scope"] = scope
    if str(refreshed.get("status") or "") != "ready":
        return refreshed, {"installed": [], "skipped": []}

    source = str(refreshed.get("source") or "").strip()
    if not source:
        refreshed.update(status="error", error="source is empty")
        return refreshed, {"installed": [], "skipped": []}

    from clio_agent.gact.agent_blueprints import install_agent_blueprint  # noqa: PLC0415

    try:
        result = install_agent_blueprint(
            source=source,
            scope=scope,
            cwd=cwd,
            ref=str(refreshed.get("ref") or ""),
            pinned_commit=str(refreshed.get("pinned_commit") or ""),
            skip_invalid=True,
        )
    except Exception as exc:  # noqa: BLE001 - persisted as an explicit source failure
        logger.warning("blueprint_source_install_failed source=%s error=%r", source, exc)
        refreshed.update(status="error", error=f"blueprint installation failed: {exc}")
        return refreshed, {"installed": [], "skipped": [], "error": str(exc)}

    installed = [dict(item) for item in result.get("installed") or [] if isinstance(item, dict)]
    skipped = [dict(item) for item in result.get("skipped") or [] if isinstance(item, dict)]
    refreshed["installed_blueprints"] = [
        {
            "id": str(item.get("id") or ""),
            "version": str(item.get("version") or ""),
            "scope": str(item.get("scope") or scope),
        }
        for item in installed
    ]
    if skipped:
        skipped_ids = ", ".join(str(item.get("id") or "unknown") for item in skipped)
        refreshed.update(
            status="degraded",
            error=f"invalid blueprint entries were not installed: {skipped_ids}",
        )
    else:
        refreshed.update(status="ready", error="")
    return refreshed, {"installed": installed, "skipped": skipped}
