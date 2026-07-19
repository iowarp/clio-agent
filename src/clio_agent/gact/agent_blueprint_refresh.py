"""Default-registry upgrade path: evaluate and repair a stale install (#948 S4b).

Owner module (no accretion into ``agent_blueprints.py``) for the decision the
bootstrap makes when the default Agent Blueprint is *already installed*: is the
snapshot healthy, pin-mismatched, or the pre-migration **stale-invalid** case --
a ``chain_of_thought``/``predict`` root that declares children and is therefore
disabled by hierarchy validation (only a ``react`` root can reach children via
the spawn-runtime tools).

An install left disabled is a dead end: the box fails every default session with
a typed ``blueprint_root_disabled`` and never self-heals, so each upgraded box
would need manual surgery. The shipped registry pin follows marketplace ``main``
HEAD, which now carries the migrated ``react`` packs, so this module re-runs the
bootstrap install from the remote to replace the stale snapshot. It is a TYPED,
logged migration -- never silent -- and the stale copy is kept intact if the
refresh fails (offline, clone error, still-invalid remote).

All names from :mod:`clio_agent.gact.agent_blueprints` are imported at module
top level; that module imports THIS one only function-locally (inside
``ensure_default_registry_bootstrap``), so there is no import cycle.
"""

from __future__ import annotations

import logging
from pathlib import Path

from clio_agent.gact.agent_blueprints import (
    DEFAULT_AGENT_BLUEPRINT_ID,
    DEFAULT_REGISTRY_REF,
    default_registry_install_source,
    default_registry_url,
    install_agent_blueprint,
    read_install_metadata,
    validate_agent_blueprint_path,
)

logger = logging.getLogger(__name__)


def evaluate_installed_default_registry(
    *,
    home: Path,
    cwd: Path,
    root: Path,
    pinned: str,
) -> str:
    """Decide what an already-installed default blueprint needs, and act.

    Precedence: a stale-invalid install (declared root disabled by validation) is
    refreshed from the remote first; otherwise the existing HEAD-following /
    pinned-commit contract applies unchanged.

    Args:
        home: Injected per-user home (test DI; production ``Path.home()``).
        cwd: Workspace directory for install resolution.
        root: The installed default blueprint directory (contains ``AGENT.md``).
        pinned: The pinned commit (empty string => follow the registry ref HEAD).

    Returns:
        An empty string when the install is acceptable (healthy, pin-matched, or
        successfully refreshed), otherwise a human-readable diagnostic that
        discovery surfaces.
    """

    disabled, root_errors = _default_blueprint_root_disabled(root)
    if disabled:
        return _refresh_stale_default_registry_install(
            home=home, cwd=cwd, root=root, root_errors=root_errors, pinned=pinned
        )
    # HEAD-following mode (no pinned commit): any installed snapshot is
    # acceptable; we track the registry ref rather than a frozen commit.
    if not pinned:
        return ""
    metadata = read_install_metadata(root)
    installed_commit = str(metadata.get("commit") or "").strip()
    if installed_commit == pinned:
        return ""
    if installed_commit:
        return (
            f"default registry pin mismatch for {DEFAULT_AGENT_BLUEPRINT_ID}: "
            f"expected {pinned}, found {installed_commit}"
        )
    return (
        f"default registry install metadata missing pinned commit for {DEFAULT_AGENT_BLUEPRINT_ID}"
    )


def _default_blueprint_root_disabled(root: Path) -> tuple[bool, list[str]]:
    """Report whether an installed default blueprint's declared root is disabled.

    Reuses the shared load+validate pipeline (:func:`validate_agent_blueprint_path`)
    -- no duplicated validation logic -- to detect the pre-migration stale-install
    case: the default blueprint is present but its declared ``root_expert`` is left
    disabled by hierarchy/tool validation (e.g. a ``chain_of_thought``/``predict``
    root that declares children, which only a ``react`` root can reach). This is
    the same fact the turn path fails TYPED on (``_BlueprintRootDisabled``).

    Args:
        root: The installed blueprint directory (containing ``AGENT.md``).

    Returns:
        ``(disabled, root_validation_errors)``. A blueprint that declares no
        ``root_expert`` -- or whose declared root validates enabled -- is not
        stale-invalid and returns ``(False, [])``.
    """

    try:
        validation = validate_agent_blueprint_path(root, scope="global")
    except Exception as exc:  # noqa: BLE001 - a parse failure is not the stale-root case
        logger.debug("default blueprint root validation skipped for %s: %r", root, exc)
        return False, []
    blueprint = validation.get("agent_blueprint") or {}
    root_expert = str(blueprint.get("root_expert") or "").strip()
    if not root_expert:
        return False, []
    for row in validation.get("agents") or []:
        if str(row.get("id") or "") == root_expert:
            if row.get("enabled"):
                return False, []
            return True, [str(error) for error in row.get("validation_errors") or []]
    return True, [f"root_expert not found: {root_expert}"]


def _refresh_stale_default_registry_install(
    *,
    home: Path,
    cwd: Path,
    root: Path,
    root_errors: list[str],
    pinned: str,
) -> str:
    """Re-install the default registry snapshot over a stale-invalid install.

    Safety: :func:`install_agent_blueprint` clones and re-validates the
    replacement in a private temp dir and only swaps it over the existing install
    after that succeeds, so a failed refresh (offline, clone error, still-invalid
    remote) never deletes the only copy -- the stale install is kept and the
    existing typed ``blueprint_root_disabled`` diagnostic keeps surfacing
    downstream. Every outcome emits a structured, logged reason (no silent
    fallback).

    Returns:
        An empty string when the refresh repaired the root, otherwise a
        human-readable diagnostic describing the kept-stale / still-disabled case.
    """

    registry = default_registry_url()
    old_commit = str(read_install_metadata(root).get("commit") or "").strip() or "(unknown)"
    try:
        result = install_agent_blueprint(
            source=default_registry_install_source(),
            scope="global",
            cwd=cwd,
            home=home,
            ref=DEFAULT_REGISTRY_REF,
            blueprint_id=DEFAULT_AGENT_BLUEPRINT_ID,
            pinned_commit=pinned,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "default_registry_refresh_failed reason=root_disabled_stale_install "
            "old_commit=%s registry=%s error=%r",
            old_commit,
            registry,
            exc,
        )
        detail = "; ".join(root_errors) or "root disabled"
        return (
            f"default registry {DEFAULT_AGENT_BLUEPRINT_ID} root expert is disabled by "
            f"validation ({detail}) and the refresh from {registry} failed: {exc}; "
            "stale install kept"
        )
    new_commit = ""
    for installed in result.get("installed") or []:
        if not isinstance(installed, dict) or installed.get("id") != DEFAULT_AGENT_BLUEPRINT_ID:
            continue
        meta = installed.get("install")
        if isinstance(meta, dict):
            new_commit = str(meta.get("commit") or "").strip()
        break
    logger.warning(
        "default_registry_refreshed reason=root_disabled_stale_install "
        "old_commit=%s new_commit=%s registry=%s",
        old_commit,
        new_commit or "(unknown)",
        registry,
    )
    disabled_after, errors_after = _default_blueprint_root_disabled(root)
    if disabled_after:
        # The remote snapshot is ALSO stale-invalid (registry not yet migrated):
        # the refresh ran but did not repair the declared root. The freshly
        # installed copy is kept; surface the still-disabled diagnostic loudly.
        detail = "; ".join(errors_after) or "root disabled"
        return (
            f"default registry {DEFAULT_AGENT_BLUEPRINT_ID} refreshed from {registry} "
            f"(new_commit={new_commit or '(unknown)'}) but the root expert is still "
            f"disabled by validation ({detail})"
        )
    return ""
