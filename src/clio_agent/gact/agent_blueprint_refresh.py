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
import threading
from pathlib import Path

from clio_agent import conf
from clio_agent.gact.agent_blueprint_sources import record_default_agent_blueprint_source
from clio_agent.gact.agent_blueprints import (
    _BLUEPRINT_ROOT_NAME,
    DEFAULT_AGENT_BLUEPRINT_ID,
    DEFAULT_REGISTRY_COMMIT,
    DEFAULT_REGISTRY_REF,
    _install_root,
    default_registry_install_source,
    default_registry_url,
    install_agent_blueprint,
    read_install_metadata,
    validate_agent_blueprint_path,
)

logger = logging.getLogger(__name__)


def ensure_default_registry_bootstrap(
    *,
    home: Path | None = None,
    cwd: Path | None = None,
) -> str:
    """Ensure the WHOLE default registry set is installed (all of marketplace main).

    The registry is the shipped standard library, not a catalog to shop from
    (owner ruling 2026-08-13): first-run installs every valid pack in the
    registry, and when the registry source is a local checkout (the dev
    submodule) each boot cheaply installs any pack the registry has gained
    since. The #948 S4b staleness evaluation of the default blueprint is
    unchanged. Returns an empty string on success or when bootstrap is
    disabled, otherwise a human-readable diagnostic that discovery surfaces as
    a disabled blueprint row instead of silently falling back.
    """

    import os as _os  # noqa: PLC0415 - keep the moved body byte-stable

    if conf.resolve(
        "agents.disable_default_registry_bootstrap",
        # Literal (not the cross-module ``_DEFAULT_BOOTSTRAP_ENV`` re-export):
        # scripts/gen_env_reference.py's AST walk only resolves module
        # constants defined IN THE SAME FILE it is scanning, so an imported
        # reference here silently dropped this knob from the generated
        # docs/config.defaults.yaml since the #948 S4b split moved this call
        # site out of agent_blueprints.py without inlining the value.
        env="CLIO_AGENT_DISABLE_DEFAULT_REGISTRY_BOOTSTRAP",
        default=False,
        cast=conf.as_bool,
    ):
        return ""
    home = home or Path.home()
    cwd = cwd or Path(_os.getcwd())
    pinned = DEFAULT_REGISTRY_COMMIT.strip()
    source = default_registry_install_source()
    root = _install_root(home=home, cwd=cwd, scope="global") / DEFAULT_AGENT_BLUEPRINT_ID
    sync_diagnostic = sync_local_registry_packs(source=source, home=home, cwd=cwd, pinned=pinned)
    if (root / _BLUEPRINT_ROOT_NAME).exists():
        # #948 S4b upgrade path: an installed-but-invalid default blueprint (a
        # pre-migration chain_of_thought/predict root disabled by validation) is
        # a dead end that never self-heals.
        diagnostic = (
            evaluate_installed_default_registry(home=home, cwd=cwd, root=root, pinned=pinned)
            or sync_diagnostic
        )
        if not diagnostic:
            _record_default_registry_source(
                source=source,
                home=home,
                cwd=cwd,
                pinned=pinned,
            )
        return diagnostic
    try:
        result = install_agent_blueprint(
            source=source,
            scope="global",
            cwd=cwd,
            home=home,
            ref=DEFAULT_REGISTRY_REF,
            blueprint_id="",
            pinned_commit=pinned,
            skip_invalid=True,
        )
    except Exception as exc:  # noqa: BLE001
        target = pinned or DEFAULT_REGISTRY_REF
        return f"unable to install default registry {default_registry_url()}@{target}: {exc}"
    if not (root / _BLUEPRINT_ROOT_NAME).exists():
        skipped = {
            str(row.get("id")): "; ".join(row.get("validation_errors") or [])
            for row in result.get("skipped") or []
            if isinstance(row, dict)
        }
        detail = skipped.get(DEFAULT_AGENT_BLUEPRINT_ID) or "not present in the registry"
        return (
            f"default registry install from {default_registry_url()} did not produce "
            f"{DEFAULT_AGENT_BLUEPRINT_ID}: {detail}"
        )
    _record_default_registry_source(source=source, home=home, cwd=cwd, pinned=pinned)
    return sync_diagnostic


def _record_default_registry_source(*, source: str, home: Path, cwd: Path, pinned: str) -> None:
    """Expose the automatically installed marketplace through source discovery."""

    try:
        record_default_agent_blueprint_source(
            source=source,
            ref=DEFAULT_REGISTRY_REF,
            pinned_commit=pinned,
            install_root=_install_root(home=home, cwd=cwd, scope="global"),
        )
    except Exception as exc:  # noqa: BLE001 - installed packs remain usable
        logger.warning("default_registry_source_record_failed source=%s error=%r", source, exc)


def update_installed_agent_blueprint(
    *,
    blueprint_id: str,
    scope: str,
    cwd: Path,
    home: Path | None = None,
) -> dict:
    """Re-install one blueprint from its recorded install source (lifecycle op)."""

    root = _install_root(home=home or Path.home(), cwd=cwd, scope=scope) / blueprint_id
    metadata = read_install_metadata(root)
    source = str(metadata.get("source") or "").strip()
    if not source:
        raise ValueError(f"agent blueprint {blueprint_id!r} has no install source metadata")
    return install_agent_blueprint(
        source=source,
        scope=scope,  # type: ignore[arg-type]
        cwd=cwd,
        home=home,
        ref=str(metadata.get("ref") or ""),
        blueprint_id=blueprint_id,
    )


def uninstall_agent_blueprint(
    *,
    blueprint_id: str,
    scope: str,
    cwd: Path,
    home: Path | None = None,
) -> dict:
    """Remove one installed blueprint; a global uninstall is tombstoned as durable.

    The tombstone is what keeps :func:`sync_local_registry_packs` from
    resurrecting the pack on the next boot (review 2026-08-13 blocker — a
    delete that silently undoes itself on the next discovery call).
    """

    import shutil as _shutil  # noqa: PLC0415

    home = home or Path.home()
    root = _install_root(home=home, cwd=cwd, scope=scope) / blueprint_id
    if not root.exists():
        raise FileNotFoundError(f"installed agent blueprint not found: {blueprint_id}")
    _shutil.rmtree(root)
    if scope == "global":
        tombstones = read_uninstalled_tombstones(home=home, cwd=cwd)
        tombstones.add(blueprint_id)
        write_uninstalled_tombstones(tombstones, home=home, cwd=cwd)
    return {"uninstalled": {"id": blueprint_id, "scope": scope, "root": str(root)}}


def uninstalled_tombstones_path(*, home: Path, cwd: Path) -> Path:
    """The per-user ledger of blueprint ids the USER uninstalled (sync must skip)."""

    return _install_root(home=home, cwd=cwd, scope="global") / ".uninstalled.json"


def read_uninstalled_tombstones(*, home: Path, cwd: Path) -> set[str]:
    """Blueprint ids the user uninstalled — the registry sync never resurrects these."""

    import json as _json  # noqa: PLC0415

    path = uninstalled_tombstones_path(home=home, cwd=cwd)
    try:
        payload = _json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set()
    except Exception:  # noqa: BLE001 - a corrupt ledger must not break discovery
        logger.warning("uninstall tombstone ledger unreadable path=%s", path)
        return set()
    ids = payload.get("uninstalled") if isinstance(payload, dict) else payload
    if not isinstance(ids, list):
        return set()
    return {str(item) for item in ids if str(item).strip()}


def write_uninstalled_tombstones(ids: set[str], *, home: Path, cwd: Path) -> None:
    """Persist the uninstall ledger (created on first uninstall, pruned on install)."""

    import json as _json  # noqa: PLC0415

    path = uninstalled_tombstones_path(home=home, cwd=cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps({"uninstalled": sorted(ids)}, indent=2), encoding="utf-8")


# The per-boot registry sync is boot semantics, not per-request semantics: the
# result cannot change within one process (the submodule does not move under a
# running serve), and discovery calls it on every request. Gate it to once per
# process; tests reset via :func:`reset_registry_sync_for_tests`.
_SYNC_COMPLETED_FOR: set[tuple[str, str]] = set()
_SYNC_LOCK = threading.Lock()


def reset_registry_sync_for_tests() -> None:
    """Clear the once-per-process sync gate (test isolation)."""

    _SYNC_COMPLETED_FOR.clear()


def sync_local_registry_packs(*, source: str, home: Path, cwd: Path, pinned: str) -> str:
    """Install registry packs missing from the global root (local-path sources only).

    A local registry checkout (the dev submodule) makes enumeration free, so a
    pack added to the registry after the original bootstrap (the
    ``earthscope-flat`` case: added to main 2026-08-05, box enumerated 07-01)
    installs on the next boot instead of never. Remote (git URL) sources skip
    this — their set is reconciled on first-run and manual installs, never via
    a per-boot network fetch. Guarantees:

    * only MISSING ids install — an installed pack is never reinstalled here
      (no clobbering of local edits, no downgrades from a stale submodule);
    * a pack the USER uninstalled (the tombstone ledger) is never resurrected;
    * the whole body is failure-isolated: any error is a logged, returned
      diagnostic, never an exception into discovery (every blueprint route sits
      downstream of this call);
    * runs once per process per (source, install root) — boot semantics.
    """

    from clio_agent.gact.agent_blueprints import (  # noqa: PLC0415 - cycle-free at call time
        _install_candidates,
        parse_agent_blueprint_root,
    )

    try:
        source_path = Path(source).expanduser()
        if not source_path.exists() or not source_path.is_dir():
            return ""
        install_root = _install_root(home=home, cwd=cwd, scope="global")
        gate_key = (str(source_path), str(install_root))
        # Discovery can run concurrently during first browser connection. The
        # old set-only gate allowed every caller through until one completed,
        # so they rmtree/copytree'd the same pack destinations concurrently and
        # produced partial installs. Serialize the complete reconcile and check
        # the gate again after acquiring the lock.
        with _SYNC_LOCK:
            if gate_key in _SYNC_COMPLETED_FOR:
                return ""
            tombstones = read_uninstalled_tombstones(home=home, cwd=cwd)
            failures: list[str] = []
            for candidate in _install_candidates(source_path):
                parsed = parse_agent_blueprint_root(candidate, scope="install")
                if not parsed.enabled:
                    continue
                if parsed.id in tombstones:
                    logger.info("registry_pack_skipped reason=user_uninstalled id=%s", parsed.id)
                    continue
                if (install_root / parsed.id / _BLUEPRINT_ROOT_NAME).exists():
                    continue
                try:
                    install_agent_blueprint(
                        source=source,
                        scope="global",
                        cwd=cwd,
                        home=home,
                        ref=DEFAULT_REGISTRY_REF,
                        blueprint_id=parsed.id,
                        pinned_commit=pinned,
                    )
                    logger.info(
                        "registry_pack_installed reason=missing_from_install_root id=%s source=%s",
                        parsed.id,
                        source,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "registry_pack_install_failed id=%s source=%s error=%r",
                        parsed.id,
                        source,
                        exc,
                    )
                    failures.append(f"{parsed.id}: {exc}")
            _SYNC_COMPLETED_FOR.add(gate_key)
            if failures:
                return "unable to install registry pack(s): " + "; ".join(failures)
            return ""
    except Exception as exc:  # noqa: BLE001 - discovery must never die on registry sync
        logger.warning("registry_sync_failed source=%s error=%r", source, exc)
        return f"registry sync failed: {exc}"


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
