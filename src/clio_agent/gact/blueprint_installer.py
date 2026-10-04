"""Materialize and validate marketplace snapshots before replacing installed copies."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from clio_agent.gact.agent_blueprints import (
    _install_candidates,
    _install_root,
    parse_agent_blueprint_root,
)
from clio_agent.gact.blueprint_git import run_git
from clio_agent.gact.blueprint_identity import install_destination
from clio_agent.gact.blueprint_install_files import read_install_metadata
from clio_agent.gact.blueprint_install_revision import InstallRevision
from clio_agent.gact.blueprint_runtime_preparation import (
    prepare_blueprint_runtime,
    require_unchanged_runtime,
)
from clio_agent.gact.git_source import normalize_git_clone_source

logger = logging.getLogger(__name__)


def install_agent_blueprint(
    *,
    source: str,
    source_id: str = "",
    allow_pin_change: bool = False,
    resolved_commit: str = "",
    scope: Literal["global", "workspace"],
    cwd: Path,
    home: Path | None = None,
    ref: str = "",
    blueprint_id: str = "",
    pinned_commit: str = "",
    skip_invalid: bool = False,
    skip_blueprint_ids: Mapping[str, str] | None = None,
    app: Any | None = None,
    preserve_invalid: bool = False,
) -> dict[str, Any]:
    """Install blueprint pack(s) from ``source`` (all packs when ``blueprint_id`` is empty).

    ``skip_invalid`` governs a multi-pack install: ``False`` (explicit installs)
    keeps the strict contract — any invalid pack fails the whole call; ``True``
    (the registry bootstrap) skips invalid packs with a logged, returned
    ``skipped`` row each, so one broken marketplace entry can never veto the
    rest of the set.
    ``skip_blueprint_ids`` maps a blueprint id to the typed reason it is not installed.
    ``app`` (the install ROUTE has one) lets an overwrite-audit reason reach a semantic event.
    """
    from clio_agent.gact.agent_blueprint_refresh import clear_uninstall_tombstones, install_row
    from clio_agent.gact.agent_blueprint_requires import AgentBlueprintInstallRefused
    from clio_agent.gact.agent_blueprint_sources import source_registry_id

    home = home or Path.home()
    source_id = source_id or source_registry_id(source, ref)
    install_root = _install_root(home=home, cwd=cwd, scope=scope)
    install_root.mkdir(parents=True, exist_ok=True)
    source_path = Path(source).expanduser()
    with tempfile.TemporaryDirectory(prefix="clio-agent-blueprint-") as tmp:
        tmp_path = Path(tmp)
        resolved_source: Path
        source_kind = "path"
        commit = ""
        if source_path.exists():
            resolved_source = source_path
            try:
                commit = subprocess.check_output(
                    ["git", "-C", str(source_path), "rev-parse", "HEAD"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
            except Exception as exc:
                commit = ""
                if pinned_commit:
                    # A pin was requested but the source commit cannot be
                    # resolved: refuse to install unverified rather than let the
                    # mismatch check below fall through on the empty commit.
                    raise ValueError(
                        f"registry pin unverifiable: cannot resolve commit for "
                        f"{source_path} to verify pin {pinned_commit}: {exc!r}"
                    ) from exc
                logger.warning(
                    "registry commit unresolvable reason=registry_commit_unresolvable "
                    "path=%s error=%r",
                    source_path,
                    exc,
                )
            if pinned_commit and commit and commit != pinned_commit:
                raise ValueError(f"registry pin mismatch: expected {pinned_commit}, found {commit}")
            if (
                pinned_commit
                and subprocess.check_output(
                    ["git", "-C", str(source_path), "status", "--porcelain"], text=True
                ).strip()
            ):
                raise ValueError(
                    "pinned source has uncommitted changes; commit them or use an unpinned source"
                )
        else:
            source_kind = "git"
            checkout_commit = pinned_commit or resolved_commit
            clone_target = tmp_path / "repo"
            branch = ["--branch", ref] if ref else []
            clone_source = normalize_git_clone_source(source)  # file:// -> path (#903)
            cmd = ["git", "clone", "--depth", "1", *branch, clone_source, str(clone_target)]
            env = {
                **os.environ,
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
            }
            run_git(cmd, env=env)  # waited for while git works; typed when it stalls
            resolved_source = clone_target
            commit = subprocess.check_output(
                ["git", "-C", str(clone_target), "rev-parse", "HEAD"],
                text=True,
            ).strip()
            if checkout_commit and commit != checkout_commit:
                git_dir = ["git", "-C", str(clone_target)]
                run_git([*git_dir, "fetch", "--depth", "1", "origin", checkout_commit], env=env)
                run_git([*git_dir, "checkout", "--detach", checkout_commit], env=env)
                commit = subprocess.check_output(
                    ["git", "-C", str(clone_target), "rev-parse", "HEAD"],
                    text=True,
                ).strip()
            if checkout_commit and commit != checkout_commit:
                raise ValueError(
                    f"registry revision mismatch: expected {checkout_commit}, found {commit}"
                )
        candidates = _install_candidates(resolved_source, blueprint_id=blueprint_id)
        if not candidates:
            raise ValueError("source contains no Agent Blueprint folders with AGENT.md")
        installed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        with InstallRevision(install_root) as revision:
            staged_rows: list[tuple[Path, Path, str]] = []
            for candidate in candidates:
                parsed = parse_agent_blueprint_root(candidate, scope=scope)
                identity = f"{scope}::{source_id}::{parsed.id}"
                skips = skip_blueprint_ids or {}
                skip_reason = str(skips.get(identity) or skips.get(parsed.id, ""))
                if skip_reason:
                    logger.info("blueprint_install_skipped reason=%s id=%s", skip_reason, parsed.id)
                    skipped.append({"id": parsed.id, "reason": skip_reason})
                    continue
                if not parsed.enabled:
                    if skip_invalid:
                        logger.warning(
                            "blueprint_install_skipped reason=validation_errors id=%s "
                            "source=%s errors=%s",
                            parsed.id,
                            source,
                            "; ".join(parsed.validation_errors),
                        )
                        skipped.append(
                            {"id": parsed.id, "validation_errors": list(parsed.validation_errors)}
                        )
                        continue
                    raise AgentBlueprintInstallRefused(list(parsed.validation_errors))
                metadata = {
                    "source": source,
                    "source_id": source_id,
                    "source_kind": source_kind,
                    "ref": ref,
                    "commit": commit,
                    "pinned_commit": pinned_commit,
                    "installed_at": datetime.now(UTC).isoformat(),
                    "scope": scope,
                }
                dest = install_destination(install_root, parsed.id, metadata)
                if preserve_invalid:
                    from clio_agent.gact.agent_blueprint_refresh import (
                        _default_blueprint_root_disabled,
                    )

                    legacy = install_root / parsed.id
                    if legacy.exists() and _default_blueprint_root_disabled(legacy)[0]:
                        dest = legacy
                staged, previous_checksum = revision.stage(
                    candidate,
                    dest,
                    metadata,
                    preserve_invalid=preserve_invalid,
                    allow_pin_change=allow_pin_change,
                )
                staged_rows.append((staged, dest, previous_checksum))
            # Reject every static error before starting any candidate MCP process.
            prepared = [
                (
                    dest,
                    previous_checksum,
                    prepare_blueprint_runtime(staged, destination=dest, scope=scope, cwd=cwd),
                )
                for staged, dest, previous_checksum in staged_rows
            ]
            require_unchanged_runtime()
            revision.apply()
            for dest, previous_checksum, checks in prepared:
                row = install_row(
                    dest, scope, read_install_metadata(dest), previous_checksum, source, app=app
                )
                row["runtime_checks"] = checks
                installed.append(row)
            clear_uninstall_tombstones(installed, scope=scope, home=home, cwd=cwd)
            revision.finish()
        return {"installed": installed, "skipped": skipped}
