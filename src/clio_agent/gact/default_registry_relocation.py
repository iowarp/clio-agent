"""Fold bundled-marketplace copies from a previous CLIO install path into the current one.

The bundled marketplace's install source is the running checkout's submodule path
(:func:`clio_agent.gact.agent_blueprints.default_registry_install_source`), and a
marketplace source id is derived from that path. A CLIO started from another
directory against the same home (upgrade into a new app dir or venv, a second
checkout) therefore used to install a second copy of every bundled blueprint
under ``<src_id>--<id>``, and every bare id then resolved as
``ambiguous_blueprint`` (F054). This module treats the bundled marketplace as ONE
logical source across install paths.
"""

from __future__ import annotations

import logging
from pathlib import Path

from clio_agent.gact.agent_blueprint_sources import (
    record_default_agent_blueprint_source,
    source_registry_id,
)
from clio_agent.gact.agent_blueprints import (
    DEFAULT_REGISTRY_REF,
    DEFAULT_REGISTRY_SUBMODULE_PATH,
    parse_agent_blueprint_root,
)
from clio_agent.gact.blueprint_install_files import (
    read_install_metadata,
    tree_checksum,
    write_install_metadata,
)
from clio_agent.platform_paths import rmtree_extended

logger = logging.getLogger(__name__)


def is_bundled_registry_path(source: str) -> bool:
    """Whether ``source`` is a checkout's bundled marketplace submodule path."""
    tail = Path(DEFAULT_REGISTRY_SUBMODULE_PATH).parts
    return bool(source) and Path(source).parts[-len(tail) :] == tail


def _from_bundled_registry(metadata: dict[str, str], source: str) -> bool:
    recorded = str(metadata.get("source") or "").strip()
    return recorded == source or (
        is_bundled_registry_path(recorded)
        and metadata.get("source_kind", "path") == "path"
        and metadata.get("ref", "") in ("", DEFAULT_REGISTRY_REF)
    )


def reconcile_moved_default_registry_copies(*, source: str, install_root: Path) -> list[dict]:
    """Repoint or retire installed copies recorded from a previous bundled path.

    Runs before the per-boot registry sync, under the registry install lock. Per
    blueprint id, the legacy bare-named copy is kept (else the current-source
    copy) and its install metadata is repointed to ``source`` (typed log
    ``default_registry_copy_repointed``, ``repointed_from`` recorded); the other
    copies are removed when they carry no local edits
    (``default_registry_copy_retired``). A copy with local edits is never
    removed (``default_registry_copy_kept reason=local_edits_present``), so the
    ambiguity stays visible instead of losing the user's edits.

    Returns:
        One ``{"action", "id", "path"}`` entry per change, for callers and tests.
    """
    actions: list[dict] = []
    if not is_bundled_registry_path(source) or not install_root.is_dir():
        return actions
    source_id = source_registry_id(source, DEFAULT_REGISTRY_REF)
    try:
        groups: dict[str, list[tuple[Path, dict[str, str]]]] = {}
        for path in sorted(install_root.iterdir()):
            if not path.is_dir() or not (path / "AGENT.md").is_file():
                continue
            metadata = read_install_metadata(path)
            if _from_bundled_registry(metadata, source):
                blueprint_id = parse_agent_blueprint_root(path, scope="install").id
                groups.setdefault(blueprint_id, []).append((path, metadata))
        for blueprint_id, copies in groups.items():
            if all(metadata.get("source") == source for _, metadata in copies):
                continue
            actions.extend(_fold_copies(blueprint_id, copies, source, source_id))
    except (OSError, ValueError) as exc:
        logger.warning("default_registry_reconcile_failed source=%s error=%r", source, exc)
    return actions


def _fold_copies(
    blueprint_id: str,
    copies: list[tuple[Path, dict[str, str]]],
    source: str,
    source_id: str,
) -> list[dict]:
    actions: list[dict] = []
    keeper, kept = next(
        (copy for copy in copies if copy[0].name == blueprint_id),
        next((copy for copy in copies if copy[1].get("source") == source), copies[0]),
    )
    for path, metadata in copies:
        if path == keeper:
            continue
        recorded_sum = str(metadata.get("checksum") or "").strip()
        if recorded_sum and recorded_sum != tree_checksum(path):
            logger.warning(
                "default_registry_copy_kept reason=local_edits_present id=%s path=%s",
                blueprint_id,
                path,
            )
            actions.append({"action": "kept", "id": blueprint_id, "path": str(path)})
            continue
        rmtree_extended(path)
        logger.info(
            "default_registry_copy_retired id=%s path=%s source=%s",
            blueprint_id,
            path,
            metadata.get("source"),
        )
        actions.append({"action": "retired", "id": blueprint_id, "path": str(path)})
    if kept.get("source") != source or kept.get("source_id") != source_id:
        previous = str(kept.get("source") or "")
        repointed = {**kept, "source": source, "source_id": source_id}
        if previous != source:
            repointed["repointed_from"] = previous
        write_install_metadata(keeper, repointed)
        logger.info(
            "default_registry_copy_repointed id=%s path=%s from=%s to=%s",
            blueprint_id,
            keeper,
            previous,
            source,
        )
        actions.append({"action": "repointed", "id": blueprint_id, "path": str(keeper)})
    return actions


def record_default_registry_source(*, source: str, home: Path, cwd: Path, pinned: str) -> None:
    """Expose the automatically installed marketplace through source discovery."""
    from clio_agent.gact.agent_blueprints import _install_root  # noqa: PLC0415

    try:
        record_default_agent_blueprint_source(
            source=source,
            ref=DEFAULT_REGISTRY_REF,
            pinned_commit=pinned,
            install_root=_install_root(home=home, cwd=cwd, scope="global"),
        )
    except (OSError, ValueError) as exc:  # installed packs remain usable
        logger.warning("default_registry_source_record_failed source=%s error=%r", source, exc)
