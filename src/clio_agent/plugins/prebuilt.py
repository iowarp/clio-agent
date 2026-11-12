"""The default marketplace, installed once when the runtime is built (owner module).

Installing the pinned default marketplace fetches it with git, reads every plugin,
hashes each file, copies a snapshot and imports the trusted plugins' Python. On
Windows every one of those file opens is scanned, so an install spent 18 s on it
alone and about 54 s beside the other install steps.

A bundled runtime instead ships the result. ``build`` installs the pinned default
marketplace into ``<runtime>/marketplace`` (``state.json`` and ``snapshots/``, the
fetch cache dropped) while the runtime is built. :func:`adopt_prebuilt` then copies
those snapshots into the person's store and merges the recorded state, its paths moved
to the store, instead of installing again. It is the same content an install of the
same pin produces (the default marketplace is trusted as shipped either way), so the
store, the trust record and the next start are unchanged. A runtime without the
prebuilt store, another pin or a configured source installs as before.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Any

from clio_agent import path_relocation

logger = logging.getLogger(__name__)

#: ``<runtime>/marketplace``: the prebuilt store beside ``runtime.json``.
PREBUILT_DIR = "marketplace"


def runtime_prebuilt_dir() -> Path | None:
    """The prebuilt store of the runtime this interpreter belongs to, if it ships one."""

    candidates = [Path(sys.base_prefix).parent]
    executable = Path(sys.executable).resolve()
    candidates += [p for p in executable.parents if (p / "runtime.json").is_file()][:1]
    for root in candidates:
        folder = root / PREBUILT_DIR
        if (root / "runtime.json").is_file() and (folder / "state.json").is_file():
            return folder
    return None


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((path / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def adopt_prebuilt(
    store: Any, *, market: str, url: str, commit: str, prebuilt: Path | None = None
) -> dict[str, Any] | None:
    """Install ``market`` at ``commit`` from the runtime's prebuilt store, or ``None``.

    ``None`` means the caller installs as before: no prebuilt store, one for another
    source, pin, reader or state version, or ``market`` already installed from another
    source. The snapshots are copied before the state names them.
    """

    from clio_agent.plugins.store import (  # noqa: PLC0415
        READER_VERSION,
        STATE_VERSION,
        _copy_tree,
        _now,
    )
    from clio_agent.plugins.store_relocation import recorded_store_root  # noqa: PLC0415

    folder = prebuilt or runtime_prebuilt_dir()
    if folder is None:
        return None
    shipped = _read(folder)
    if shipped is None or shipped.get("version") != STATE_VERSION:
        return None
    markets = {
        name: entry
        for name, entry in (shipped.get("marketplaces") or {}).items()
        if name == market or entry.get("parent") == market
    }
    head = markets.get(market) or {}
    if (
        head.get("url") != url
        or head.get("commit") != commit
        or head.get("reader") != READER_VERSION
    ):
        logger.info(
            "prebuilt marketplace not used reason=different_pin marketplace=%s shipped=%s",
            market,
            head.get("commit", ""),
        )
        return None
    old_root = recorded_store_root({"marketplaces": markets})
    if old_root is None:
        return None
    started = time.monotonic()
    for name in markets:
        source = folder / "snapshots" / name
        for snapshot in sorted(source.iterdir()) if source.is_dir() else []:
            if snapshot.is_dir():
                _copy_tree(snapshot, store.root / "snapshots" / name / snapshot.name)
    moves = [(old_root, str(store.root))]
    moved: dict[str, Any] = path_relocation._relocate_value(
        {key: shipped.get(key) or {} for key in ("marketplaces", "plugins", "pending", "agents")},
        moves,
    )
    ours = set(markets)
    with store.lock:
        state = store.read_state()
        for name in ours:
            previous = state["marketplaces"].get(name) or {}
            if previous and previous.get("url") != moved["marketplaces"][name].get("url"):
                return None  # the name is another source's: install reports it
        for section in ("plugins", "pending", "agents"):
            for key in [k for k in state[section] if k.split("/", 1)[0] in ours]:
                del state[section][key]
            state[section].update(
                {k: v for k, v in moved[section].items() if k.split("/", 1)[0] in ours}
            )
        for name in ours:
            entry = dict(moved["marketplaces"][name])
            previous = state["marketplaces"].get(name) or {}
            entry["installed_at"] = previous.get("installed_at") or _now()
            entry["updated_at"] = _now()
            state["marketplaces"][name] = entry
        store._write_state(state)
    plugins = sorted(k.split("/", 1)[1] for k in moved["plugins"] if k.split("/", 1)[0] == market)
    summary = {
        "marketplace": market,
        "format": head.get("format", ""),
        "trusted": bool(head.get("trusted")),
        "plugins": plugins,
        "pending_trust": [],
        "agents": sorted(
            k.split("/", 1)[1] for k in moved["agents"] if k.split("/", 1)[0] == market
        ),
        "diagnostics": list(head.get("diagnostics") or []),
        "prebuilt": True,
    }
    logger.info(
        "prebuilt marketplace adopted marketplace=%s commit=%s plugins=%d seconds=%.1f",
        market,
        commit,
        len(plugins),
        time.monotonic() - started,
    )
    return summary


def _writable_retry(function: Any, path: str, _exc: BaseException) -> None:
    # git writes its object files read-only, which Windows refuses to delete.
    os.chmod(path, stat.S_IWRITE)
    function(path)


def build(out: Path) -> dict[str, Any]:
    """Install the pinned default marketplace into ``out`` and keep only what adopt reads."""

    from clio_agent.plugins.service import (  # noqa: PLC0415
        DEFAULT_MARKETPLACE,
        DEFAULT_MARKETPLACE_COMMIT,
        DEFAULT_MARKETPLACE_URL,
    )
    from clio_agent.plugins.store import PluginStore  # noqa: PLC0415

    if out.exists():
        shutil.rmtree(out, onexc=_writable_retry)
    store = PluginStore(out.resolve())
    summary = store.install(
        DEFAULT_MARKETPLACE_URL,
        commit=DEFAULT_MARKETPLACE_COMMIT,
        name=DEFAULT_MARKETPLACE,
        default=True,
    )
    errors = [d for d in summary.get("diagnostics", []) if d.get("severity") == "error"]
    if errors or summary.get("pending_trust"):
        raise SystemExit(
            f"prebuilt marketplace: the default marketplace did not install: {summary}"
        )
    for name in os.listdir(out):
        if name not in {"state.json", "snapshots"}:
            target = out / name
            if target.is_dir():
                shutil.rmtree(target, onexc=_writable_retry)
            else:
                target.unlink()
    for folder, dirs, _files in os.walk(out, topdown=True):
        for name in [d for d in dirs if d == "__pycache__"]:
            shutil.rmtree(Path(folder) / name)
            dirs.remove(name)
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["build"])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    started = time.monotonic()
    summary = build(args.out)
    print(
        f"[prebuilt-marketplace] {summary['marketplace']}: {len(summary['plugins'])} plugins, "
        f"{len(summary['agents'])} agents in {time.monotonic() - started:.1f}s -> {args.out}",
        flush=True,
    )


if __name__ == "__main__":
    main()
