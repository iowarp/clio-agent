"""Tiered execution-environment identity for TransformRecords (#966.6 / S5 #971).

A TransformRecord stamps the environment it ran in so its replay guarantee is
honest and permanent. The shape mirrors
``evidence.py::_dynamic_agent_runtime_provenance`` (non-secret provenance only):
clio ``__version__``, ``sha256(uv.lock)`` (the ``lockfile-hash`` tier), the
clio-kit launcher fingerprint (the MCP listing-cache ``size:mtime`` — a proxy for
the tool-fleet state), the active provider/model ids, and os/arch/python.

Tiers (owner decision #966.6), strongest last:

* ``declared`` — clio version only (no lockfile reachable);
* ``lockfile-hash`` — clio version + ``sha256(uv.lock)`` (the reproducibility
  floor S5's replay contract requires);
* ``image-digest`` — a container image digest (RESERVED — Campaign B / a future
  packaging slice mints it; nothing here does, exactly as ``plan`` kind is
  reserved in :mod:`records`).

The replay contract is stamped ON the record and is permanent (never silently
upgraded): a transform is **reproducible** iff its environment tier is at least
``lockfile-hash`` AND every used input is content-pinned; otherwise it is
**re-runnable** — the run is described but bit-identical replay is not
guaranteed. Every degrade carries a typed reason (no silent fallback).
"""

from __future__ import annotations

import hashlib
import logging
import platform
import re
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)

_HASH_CHUNK_BYTES = 1024 * 1024


class EnvironmentTier(str, Enum):
    """How precisely the execution environment is pinned (owner decision #966.6).

    A total order (``declared`` < ``lockfile-hash`` < ``image-digest``) so the
    replay contract can test ``tier >= lockfile-hash`` with one comparison.
    ``image-digest`` is RESERVED — nothing mints it this campaign.
    """

    DECLARED = "declared"
    LOCKFILE_HASH = "lockfile-hash"
    IMAGE_DIGEST = "image-digest"


#: The tier total order (owner decision #966.6). Higher wins.
_TIER_RANK: dict[EnvironmentTier, int] = {
    EnvironmentTier.DECLARED: 0,
    EnvironmentTier.LOCKFILE_HASH: 1,
    EnvironmentTier.IMAGE_DIGEST: 2,
}


def tier_at_least(tier: EnvironmentTier, floor: EnvironmentTier) -> bool:
    """Whether ``tier`` is at least as strong as ``floor`` in the tier order."""
    return _TIER_RANK[tier] >= _TIER_RANK[floor]


class EnvironmentRecord(BaseModel):
    """Non-secret, tiered execution-environment identity for one transform.

    Pure value (frozen); the harness fills it, the model is never load-bearing.
    Mirrors the ``_dynamic_agent_runtime_provenance`` shape: nothing here is a
    secret (no API keys, no auth tokens) — only version/lockfile/launcher/os
    fingerprints and the active provider/model ids.
    """

    model_config = ConfigDict(frozen=True)

    tier: EnvironmentTier = EnvironmentTier.DECLARED
    clio_version: str = ""
    #: ``sha256(uv.lock)`` resolved at clio's repo-root anchor, else ``""`` (a typed
    #: ``lockfile_unavailable`` reason is logged and the tier stays ``declared``).
    lockfile_sha256: str = ""
    #: The MCP listing-cache ``size:mtime`` fingerprint — a clio-kit launcher/
    #: tool-fleet proxy; ``""`` when the cache file is absent.
    launcher_fingerprint: str = ""
    provider_id: str = ""
    model_id: str = ""
    model_variant: str = ""
    #: How the model ref was resolved (finding [8]): ``executing_lm`` — the LM bound
    #: by the executing expert's ``dspy.context``; ``global_fallback`` — the app-bound
    #: global LM (no per-profile context was active on the recording thread).
    model_source: str = ""
    os: str = ""
    arch: str = ""
    python_version: str = ""
    #: A reserved container image digest (never minted this campaign).
    image_digest: str = ""


_CLIO_PACKAGE_NAME = "clio-agent"


def _pyproject_names_clio(text: str) -> bool:
    """Whether a ``pyproject.toml``'s ``[project]`` declares ``name = "clio-agent"``.

    A tolerant line match (no TOML parser dep): the first ``name = "..."`` assignment
    is the project name under ``[project]`` in clio's own root pyproject.
    """
    for line in text.splitlines():
        match = re.match(r"""\s*name\s*=\s*["']([^"']+)["']""", line)
        if match:
            return match.group(1).strip() == _CLIO_PACKAGE_NAME
    return False


def _clio_repo_root(start: Optional[Path] = None) -> Optional[Path]:
    """The clio-agent repo root — the ``__file__`` ancestor whose pyproject names clio.

    An EXPLICIT anchor (finding [6]), never a first-``uv.lock`` directory walk: only a
    directory carrying a ``pyproject.toml`` with ``name = "clio-agent"`` is clio's own
    root, so a nested/packaged install can never mistake an unrelated ancestor's
    lockfile for clio's environment identity. Returns ``None`` when no such anchor
    exists above this module (a wheel/pip install). ``start`` overrides the module
    path for testing a synthetic install layout.
    """
    here = (start or Path(__file__)).resolve()
    for parent in here.parents:
        pyproject = parent / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            continue
        if _pyproject_names_clio(text):
            return parent
    return None


def _lockfile_path(start: Optional[Path] = None) -> Optional[Path]:
    """Locate clio's OWN ``uv.lock`` at its repo-root anchor, or ``None`` (finding [6]).

    Resolved ONLY at the explicit clio-agent repo-root anchor
    (:func:`_clio_repo_root`), NEVER by a directory walk that could hash an unrelated
    ancestor lockfile in a nested/packaged install. A wheel/pip install (no
    clio-agent pyproject anchor) → ``None`` → the tier honestly falls back to
    ``declared`` with a typed ``lockfile_unavailable`` reason.
    """
    root = _clio_repo_root(start)
    if root is None:
        return None
    candidate = root / "uv.lock"
    return candidate if candidate.is_file() else None


def _sha256_file(path: Path) -> Optional[str]:
    """Stream a file's sha256 (bounded memory), or ``None`` on a read failure."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(_HASH_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _lockfile_hash() -> str:
    """Return ``sha256(uv.lock)`` or ``""`` (with a typed reason logged)."""
    path = _lockfile_path()
    if path is None:
        logger.info("environment tier declared reason=lockfile_unavailable detail=no_clio_anchor")
        return ""
    digest = _sha256_file(path)
    if digest is None:
        logger.warning("environment tier declared reason=lockfile_unreadable path=%s", path)
        return ""
    return digest


def _launcher_fingerprint() -> str:
    """The MCP listing-cache ``size:mtime`` fingerprint (clio-kit launcher proxy).

    Empty when the cache file is absent (a fresh boot before the first MCP
    listing) — an honest empty, not a degrade.
    """
    try:
        from clio_agent import paths  # noqa: PLC0415

        cache = paths.user_cache_dir() / "mcp_listing_cache.json"
        if not cache.is_file():
            return ""
        stat = cache.stat()
        return f"{int(stat.st_size)}:{stat.st_mtime:.0f}"
    except Exception:  # noqa: BLE001 — a launcher fingerprint is best-effort provenance
        return ""


def _model_ref_from_lm(lm: Any) -> dict[str, str]:
    """Project a bound dspy LM object to a provider/model ref (``model`` is ``prov/id``)."""
    model = str(getattr(lm, "model", "") or "")
    provider, _, ident = model.partition("/")
    if ident:
        return {"provider_id": provider, "model_id": model, "variant": ""}
    return {"provider_id": "", "model_id": model, "variant": ""}


def _active_model(app: "FastAPI") -> dict[str, str]:
    """The EXECUTING model ref, preferring the expert's bound LM (finding [8]).

    A leaf expert runs its own model via ``dspy.context(lm=...)`` (per-expert
    provider), which does NOT mutate the app-bound global. So we first read the LM
    actually bound on this thread (:func:`ambient_lm.active_lm`, ``ambient=False``);
    only when no per-profile context is active do we fall back to the global ref —
    a TYPED fallback (``model_source=global_fallback``), never a silent wrong stamp.
    """
    try:
        from clio_agent.gact.runtime.ambient_lm import active_lm  # noqa: PLC0415

        lm, ambient = active_lm()
        if lm is not None and not ambient:
            ref = _model_ref_from_lm(lm)
            if ref.get("model_id"):
                return {**ref, "source": "executing_lm"}
    except Exception:  # noqa: BLE001 — executing-LM read is best-effort → typed global fallback
        logger.debug(
            "environment model ref reason=executing_lm_unreadable falling_back=global",
            exc_info=True,
        )
    try:
        from clio_agent.gact.providers.config import _active_lm_model_ref  # noqa: PLC0415

        return {**_active_lm_model_ref(app), "source": "global_fallback"}
    except Exception:  # noqa: BLE001 — model provenance is best-effort, never load-bearing
        return {"provider_id": "", "model_id": "", "variant": "", "source": "global_fallback"}


def capture_environment(app: "FastAPI") -> EnvironmentRecord:
    """Capture the tiered execution-environment identity for the current turn.

    Best-effort by construction: the strongest reachable tier is used. With
    ``uv.lock`` reachable the tier is ``lockfile-hash`` (the reproducibility
    floor); otherwise ``declared`` (clio version only) with a typed reason. No
    secret ever enters the record (mirrors ``_dynamic_agent_runtime_provenance``).
    """
    from clio_agent import __version__ as clio_version  # noqa: PLC0415

    lockfile_sha = _lockfile_hash()
    tier = EnvironmentTier.LOCKFILE_HASH if lockfile_sha else EnvironmentTier.DECLARED
    model = _active_model(app)
    return EnvironmentRecord(
        tier=tier,
        clio_version=str(clio_version or ""),
        lockfile_sha256=lockfile_sha,
        launcher_fingerprint=_launcher_fingerprint(),
        provider_id=str(model.get("provider_id") or ""),
        model_id=str(model.get("model_id") or ""),
        model_variant=str(model.get("variant") or ""),
        model_source=str(model.get("source") or ""),
        os=platform.system(),
        arch=platform.machine(),
        python_version=platform.python_version(),
    )


def environment_from_payload(raw: Any) -> EnvironmentRecord:
    """Rebuild an :class:`EnvironmentRecord` from a folded payload dict (tolerant).

    Unknown tier strings fall back to ``declared`` (never a crash at boot fold).
    """
    data: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    try:
        tier = EnvironmentTier(str(data.get("tier") or EnvironmentTier.DECLARED.value))
    except ValueError:
        tier = EnvironmentTier.DECLARED
    return EnvironmentRecord(
        tier=tier,
        clio_version=str(data.get("clio_version") or ""),
        lockfile_sha256=str(data.get("lockfile_sha256") or ""),
        launcher_fingerprint=str(data.get("launcher_fingerprint") or ""),
        provider_id=str(data.get("provider_id") or ""),
        model_id=str(data.get("model_id") or ""),
        model_variant=str(data.get("model_variant") or ""),
        model_source=str(data.get("model_source") or ""),
        os=str(data.get("os") or ""),
        arch=str(data.get("arch") or ""),
        python_version=str(data.get("python_version") or ""),
        image_digest=str(data.get("image_digest") or ""),
    )


__all__ = [
    "EnvironmentRecord",
    "EnvironmentTier",
    "capture_environment",
    "environment_from_payload",
    "tier_at_least",
]
