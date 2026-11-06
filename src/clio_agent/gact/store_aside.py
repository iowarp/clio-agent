"""Move a clio-core store aside, unchanged, so CLIO starts with an empty one (owner module).

An upgrade over a store this CLIO cannot open (one written by CLIO 0.9.5.1 or older,
:mod:`clio_agent.arc.store_format`) resets the CLIO state by moving it, never by
deleting it: if a converter is ever written, the old data is still there to convert,
and CLIO 0.9.5.1 opens it again once it is moved back.

What moves, by rename on the same disk (never a copy, never a delete):

* this machine's clio-core store folder (the default ``<data>/cte/hosts/<host>``, or
  ``arc.cte.dir``). The default store goes to
  ``<data>/cte-before-<version>-<date>/hosts/<host>``: outside ``<data>/cte``, where the
  next start's one-time move of a pre-host store (:mod:`clio_agent.arc.clio_core_host_migration`)
  never looks. An ``arc.cte.dir`` folder goes beside itself as
  ``<folder>-before-<version>-<date>``;
* the legacy session list ``sessions.json`` an older CLIO imported into that store, when
  it is still there (renamed beside itself): left in place, the new store would import
  it again and list conversations whose messages moved away.

A store named by an explicit or per-workspace ``cte.yaml`` names its own storage paths,
so it is not moved here (:class:`StoreAsideRefusedError`, ``arc_store_custom_config``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

from clio_agent.errors import ClioError

__all__ = [
    "ARC_STORE_CUSTOM_CONFIG",
    "ARC_STORE_MOVE_FAILED",
    "MovedAside",
    "StoreAsideRefusedError",
    "StoreLocation",
    "locate",
    "move_aside",
    "planned_moves",
]

#: Typed reason: the store is named by an explicit or per-workspace clio-core config.
ARC_STORE_CUSTOM_CONFIG = "arc_store_custom_config"
#: Typed reason: a rename failed (nothing was deleted; what moved is reported).
ARC_STORE_MOVE_FAILED = "arc_store_move_failed"


class StoreAsideRefusedError(ClioError):
    """The store cannot be moved aside here; the message says why and what to do."""


@dataclass(frozen=True)
class StoreLocation:
    """Where this machine's clio-core store and its legacy session list live.

    Attributes:
        store_dir: The store folder (holds ``cte.yaml`` and the file tier).
        config: The ``cte.yaml`` clio-core is started with.
        movable: The store is the default one (or ``arc.cte.dir``), which this module moves.
        default_layout: ``store_dir`` is ``<data>/cte/hosts/<host>``.
        data_dir: The agent data root (``<data>``).
        sessions_file: The legacy ``sessions.json``, when present.
    """

    store_dir: Path
    config: Path
    movable: bool
    default_layout: bool
    data_dir: Path
    sessions_file: Optional[Path]

    @property
    def exists(self) -> bool:
        """Whether a store was ever created here (its config was seeded)."""
        return self.config.is_file()


@dataclass(frozen=True)
class MovedAside:
    """What was (or would be) moved where."""

    store_from: Path
    store_to: Path
    sessions_from: Optional[Path] = None
    sessions_to: Optional[Path] = None

    def as_dict(self) -> dict[str, Any]:
        """The moves as JSON (command output)."""
        out = {"store_from": str(self.store_from), "store_to": str(self.store_to)}
        if self.sessions_from is not None and self.sessions_to is not None:
            out |= {"sessions_from": str(self.sessions_from), "sessions_to": str(self.sessions_to)}
        return out


def locate(env: Optional[dict[str, str]] = None) -> StoreLocation:
    """This machine's clio-core store, as the server would open it (read only)."""
    from clio_agent import paths  # noqa: PLC0415 - avoid import cycle
    from clio_agent.arc import clio_core_config  # noqa: PLC0415
    from clio_agent.gact.sessions import _default_store_path  # noqa: PLC0415

    store_dir = clio_core_config._default_cte_dir()
    config = clio_core_config._resolve_config_path(env if env is not None else os.environ)
    data_dir = paths.user_data_dir()
    sessions = _default_store_path()
    return StoreLocation(
        store_dir=store_dir,
        config=config,
        movable=config == store_dir / "cte.yaml",
        default_layout=store_dir == data_dir / "cte" / "hosts" / clio_core_config.host_key(),
        data_dir=data_dir,
        sessions_file=sessions if sessions.is_file() else None,
    )


def _unique(path: Path, make: Any) -> Path:
    """``path``, or the first ``make(n)`` (n = 2, 3, ...) that does not exist yet."""
    candidate, number = path, 1
    while candidate.exists():
        number += 1
        candidate = make(number)
    return candidate


def planned_moves(location: StoreLocation, version: str, day: Optional[date] = None) -> MovedAside:
    """Where :func:`move_aside` would put the store and the session list (no change).

    Raises:
        StoreAsideRefusedError: The store is named by its own clio-core config.
    """
    if not location.movable:
        raise StoreAsideRefusedError(
            f"This CLIO starts clio-core with {location.config}, which names its own storage "
            "folders, so it is not moved here. Stop CLIO and move the folders that file "
            "names aside yourself (rename them; do not delete them).",
            error_type=ARC_STORE_CUSTOM_CONFIG,
            details={"config": str(location.config)},
        )
    stamp = f"before-{version}-{(day or date.today()).isoformat()}"
    store = location.store_dir
    if location.default_layout:
        root = location.data_dir / f"cte-{stamp}"
        tail = store.relative_to(location.data_dir / "cte")
        store_to = _unique(root / tail, lambda n: root.with_name(f"{root.name}-{n}") / tail)
    else:
        first = store.with_name(f"{store.name}-{stamp}")
        store_to = _unique(first, lambda n: first.with_name(f"{first.name}-{n}"))
    sessions = location.sessions_file
    if sessions is None:
        return MovedAside(store, store_to)
    named = sessions.with_name(f"{sessions.name}.{stamp}")
    sessions_to = _unique(named, lambda n: named.with_name(f"{named.name}-{n}"))
    return MovedAside(store, store_to, sessions, sessions_to)


def move_aside(location: StoreLocation, version: str, day: Optional[date] = None) -> MovedAside:
    """Rename the store (and the legacy session list) aside; nothing is deleted.

    The caller has made sure nothing uses the store (no live client, no daemon).

    Raises:
        StoreAsideRefusedError: The store is named by its own config, or a rename failed
            (``arc_store_move_failed``; its details say what had moved already).
    """
    moves = planned_moves(location, version, day)
    try:
        moves.store_to.parent.mkdir(parents=True, exist_ok=True)
        # A rename never copies: across disks it fails (EXDEV) instead of duplicating.
        os.rename(moves.store_from, moves.store_to)
    except OSError as exc:
        raise _move_failed(moves.store_from, moves.store_to, exc, moved=None) from exc
    if moves.sessions_from is not None and moves.sessions_to is not None:
        try:
            os.rename(moves.sessions_from, moves.sessions_to)
        except OSError as exc:
            raise _move_failed(
                moves.sessions_from, moves.sessions_to, exc, moved=moves.store_to
            ) from exc
    return moves


def _move_failed(source: Path, target: Path, exc: OSError, moved: Optional[Path]) -> ClioError:
    already = f" The store itself was moved to {moved}." if moved is not None else ""
    return StoreAsideRefusedError(
        f"Could not move {source} to {target}: {exc.strerror or exc}.{already} Nothing "
        "was deleted. Move it yourself (rename it; do not delete it), then start CLIO.",
        error_type=ARC_STORE_MOVE_FAILED,
        details={
            "from": str(source),
            "to": str(target),
            "error": repr(exc),
            **({"store_moved_to": str(moved)} if moved is not None else {}),
        },
    )
