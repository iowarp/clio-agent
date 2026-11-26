"""Codex ``-p`` layer files in the person's Codex home: write, hold, release and bound.

Codex reads a spawn's permission layer from ``<codex_home>/<layer>.config.toml`` when it
starts (see :mod:`sandbox_codex` for why the layer must live in the real ``~/.codex``). A
layer is CLIO's file in the person's home, so it stays only while a spawn needs it: each
write takes a hold, :func:`release_codex_layer` drops it when the child exits and removes the
file at the last one, and :func:`_prune_layers` bounds what a crashed process left behind.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

from .sandbox_codex_probe import codex_home_dir
from .sandbox_codex_profile import (
    CODEX_LAYER_GLOB,
    CODEX_LAYER_KEEP,
    _render_layer_toml,
    codex_layer_name,
    validate_codex_profile,
)

logger = logging.getLogger(__name__)

#: In-process holds on layer files: a layer is removed when its last spawn releases it.
_LAYER_HOLDS: dict[Path, int] = {}
_LAYER_HOLDS_LOCK = threading.Lock()


def _prune_layers(codex_home: Path, *, keep: int = CODEX_LAYER_KEEP) -> None:
    """Bound clio's layer files in the shared home to ``keep`` most-recent (no unbounded leak).

    Reaps ONLY files matching :data:`CODEX_LAYER_GLOB` (clio's ``clio-sb-*`` prefix) â€” NEVER the
    user's ``config.toml`` or any unrelated file. Released layers are removed by
    :func:`release_codex_layer`; this bound is the backstop for layers a crashed process left
    behind or a caller never released. Best-effort + guarded: a prune failure must never break a
    spawn, and a concurrent-unlink race is ignored.
    """
    try:
        files = sorted(
            (p for p in codex_home.glob(CODEX_LAYER_GLOB) if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for old in files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass  # a peer spawn may have pruned it already â€” benign


def _layer_path(layer: str, codex_home: Optional[Path | str] = None) -> Path:
    home = Path(codex_home) if codex_home else codex_home_dir()
    return home / f"{layer}.config.toml"


def release_codex_layer(layer: str, *, codex_home: Optional[Path | str] = None) -> None:
    """Drop one hold on ``layer`` and remove its file from the Codex home at the last one.

    Codex reads the layer when it starts, so a layer is needed only until every spawn that
    wrote it has exited. Idempotent per hold; a layer this process never wrote is left alone.
    """
    path = _layer_path(layer, codex_home)
    with _LAYER_HOLDS_LOCK:
        count = _LAYER_HOLDS.get(path, 0)
        if count <= 0:
            return
        if count > 1:
            _LAYER_HOLDS[path] = count - 1
            return
        del _LAYER_HOLDS[path]
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "codex layer kept reason=codex_layer_unremovable path=%s error=%s: %s",
                path,
                type(exc).__name__,
                exc,
            )


def write_codex_layer(
    profile_name: str,
    profile: dict[str, Any],
    *,
    elevated: bool,
    codex_home: Optional[Path | str] = None,
    platform: str = sys.platform,
    windows_sandbox: str = "",
) -> str:
    """Write the ``-p`` layer file into codex's DEFAULT home and return its layer name.

    The layer lives at ``<codex_home>/<clio-sb-sha8>.config.toml`` inside the REAL ``~/.codex``
    (resolved from ``codex_home`` arg, else ``$CODEX_HOME``, else ``~/.codex``) â€” a fresh/custom
    home was proven to silently drop the write grant. The ``[windows] sandbox = "elevated"``
    block is emitted ONLY when ``elevated`` on win32. Verified MXC uses its own
    explicit backend, managed proxy and content-addressed layer (a no-op elsewhere).
    Validation runs FIRST (:func:`validate_codex_profile`, typed) so a drift never reaches disk;
    the file is UTF-8 **WITHOUT a BOM** (a BOM breaks codex's TOML parser â€” verified live). The
    user's own ``config.toml`` is NEVER touched; only clio's ``clio-sb-*`` layers are pruned.
    Each call takes a hold on the layer; :func:`release_codex_layer` drops it and removes the
    file once no spawn of this process needs it. The name carries this process's id, so one
    CLIO's release never removes a layer another CLIO process is about to launch with.
    """
    validate_codex_profile(profile)
    home = Path(codex_home) if codex_home else codex_home_dir()
    home.mkdir(parents=True, exist_ok=True)
    mode = windows_sandbox if platform.startswith("win") else ""
    layer = f"{codex_layer_name(profile_name, profile, windows_sandbox=mode)}-{os.getpid()}"
    body = _render_layer_toml(
        profile_name,
        profile,
        elevated=elevated and platform.startswith("win"),
        windows_sandbox=mode,
    )
    path = home / f"{layer}.config.toml"
    with _LAYER_HOLDS_LOCK:
        # ``encoding="utf-8"`` writes NO BOM (unlike ``utf-8-sig``); a BOM breaks codex's parse.
        path.write_text(body, encoding="utf-8")
        _LAYER_HOLDS[path] = _LAYER_HOLDS.get(path, 0) + 1
    _prune_layers(home)
    return layer


def composed_layer(args: Sequence[str]) -> str:
    """The ``-p`` layer named in a :func:`compose_codex_spawn` argv ('' when there is none)."""
    head = list(args[: args.index("--")]) if "--" in args else list(args)
    if "-p" in head and head.index("-p") + 1 < len(head):
        return head[head.index("-p") + 1]
    return ""


class LayerHold:
    """One spawn's hold on its Codex ``-p`` layer, dropped once however often it is released."""

    def __init__(self, layer: str) -> None:
        self.layer = layer
        self._held = True
        self._lock = threading.Lock()

    def release(self) -> None:
        with self._lock:
            held, self._held = self._held, False
        if held:
            release_codex_layer(self.layer)


def layer_hold(args: Sequence[str]) -> LayerHold | None:
    """The hold on the layer a :func:`compose_codex_spawn` argv names, ``None`` without one."""
    layer = composed_layer(args)
    return LayerHold(layer) if layer else None


__all__ = ["LayerHold", "composed_layer", "layer_hold", "release_codex_layer", "write_codex_layer"]
