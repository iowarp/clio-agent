"""Bounded live capacity extension for CLIO's generated, disk-only CTE store.

Never resize or remove a target containing user data. A journal records a new
target's exact config and stable native ID before registration or retried writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml
from filelock import FileLock

from clio_agent import conf
from clio_agent.arc.clio_core_async_ops import await_future
from clio_agent.arc.clio_core_config import (
    _DEFAULT_CTE_CONFIG_TEMPLATE,
    _cte_yaml_path,
    _default_cte_dir,
    parse_capacity_bytes,
)
from clio_agent.arc.clio_core_durability import _PERSISTENCE_LINE
from clio_agent.arc.clio_core_indexer import indexer_block

logger = logging.getLogger(__name__)
_GIB = 1 << 30
_RESERVE = _GIB
_MAX_TARGETS = 8
_MAX_TOTAL = 500 * _GIB


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _atomic_text(path: Path, text: str) -> None:
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _managed_config(config: Path, text: str) -> dict[str, Any] | None:
    """Recognize the generated default; a hand-written config or layout opts out."""
    if not _growth_enabled(config):
        return None
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        return None
    try:
        ram, module, *indexer = document["compose"]
        (tier,) = module["storage"]
        parse_capacity_bytes(tier["capacity_limit"])
        # Older generated configs have no journal. Adopt only the unchanged seed
        # shape and owned paths, whatever capacity it was seeded with: a stale small
        # seed (an old explicit arc.cte.file_capacity such as 512MB) must grow too,
        # since agent data is never capped. Custom topologies, paths, limits or
        # policies are never rewritten; arc.cte.auto_expand=false keeps a fixed size.
        expected = _DEFAULT_CTE_CONFIG_TEMPLATE.format(
            core_port=document["networking"]["port"],
            core_threads=document["runtime"]["num_threads"],  # any count CLIO seeded
            conf_dir=_cte_yaml_path(config.parent / "conf"),
            ram_budget=ram["capacity"],
            file_tier=_cte_yaml_path(config.parent / "storage.bin"),
            file_capacity=tier["capacity_limit"],
            metadata_log=_cte_yaml_path(config.parent / "metadata.log"),
            indexer=indexer_block(_cte_yaml_path(config.parent / "indexer_index.log"))
            if indexer
            else "",
        )
    except (KeyError, TypeError, ValueError):
        return None
    return document if document == yaml.safe_load(expected) else None


def _future_result(future: Any, operation: str) -> Any:
    return await_future(future, op_name=operation).result(0)


def _additional_bytes(store: Any, tag_id: Any, name: str, payload_bytes: int) -> int:
    future = await_future(
        store._client.AsyncGetBlobSize(tag_id, name, store._cte.PoolQuery.Local()),
        op_name="capacity_blob_size",
    )
    code = future.wait(0)
    if code == 1:  # Native GetBlobSize's published not-found result.
        return payload_bytes
    if code != 0:
        raise RuntimeError(f"clio-core GetBlobSize failed during capacity recovery: code {code}")
    return max(0, payload_bytes - int(future.result(0)))


def _register(store: Any, plan: dict[str, Any]) -> None:
    index = int(plan["index"])
    future = await_future(
        store._client.AsyncRegisterTarget(
            f"{plan['path']}_node0",
            store._cte.BdevType.kFile,
            int(plan["capacity"]),
            store._cte.PoolQuery.Local(),
            store._cte.PoolId(512 + index, 1),
        ),
        op_name="capacity_register",
    )
    code = future.wait(0)
    if code != 0:
        raise RuntimeError(f"clio-core capacity target registration refused: code {code}")


def _persist_journal(path: Path, journal: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(journal, sort_keys=True, indent=2) + "\n")


def _resume_plan(store: Any, config: Path, path: Path, journal: dict[str, Any]) -> bool:
    plan = journal.get("pending")
    if not isinstance(plan, dict):
        return False
    index = int(plan["index"])
    expected_path = config.parent / f"storage-extra-{index:03d}.bin"
    planned = yaml.safe_load(plan["config_text"])
    tiers = planned["compose"][1]["storage"]
    if (
        not 1 <= index < _MAX_TARGETS
        or len(tiers) != index + 1
        or Path(plan["path"]).resolve() != expected_path
        or Path(tiers[index]["path"]).resolve() != expected_path
        or parse_capacity_bytes(str(tiers[index]["capacity_limit"])) != int(plan["capacity"])
        or not 0 < int(plan["capacity"]) <= 50 * _GIB
    ):
        raise RuntimeError("Invalid CLIO capacity extension journal; refusing registration")
    digest = _digest(config.read_text(encoding="utf-8"))
    if digest == journal["config_sha256"]:
        _atomic_text(config, plan["config_text"])
    elif digest != _digest(plan["config_text"]):
        raise RuntimeError(
            "CLIO storage config changed during a capacity extension; refusing growth"
        )
    _register(store, plan)
    journal["config_sha256"] = _digest(plan["config_text"])
    journal.pop("pending")
    _persist_journal(path, journal)
    logger.warning(
        "reason=clio_core_capacity_extended config=%s path=%s capacity_bytes=%d index=%d",
        config,
        plan["path"],
        plan["capacity"],
        plan["index"],
    )
    return True


def recover_capacity(store: Any, tag_id: Any, name: str, payload_bytes: int) -> bool:
    """Extend a full managed store once, preserving every existing blob and target.

    Call only for a structured placement/allocation refusal (native code 12/13). Native free
    capacity must be insufficient for this blob's actual growth. Health/filter
    failures with sufficient space, explicit stores and explicit caps stay loud.
    No pending/unknown write is retried by this function.
    """
    config = Path(store._config_path).resolve()
    if not _growth_enabled(config):
        return False
    journal_path = config.with_name("cte-capacity.json")
    with FileLock(str(config.with_name("cte-capacity.lock")), timeout=30):
        text = config.read_text(encoding="utf-8")
        if journal_path.is_file():
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            if journal.get("schema") != 1:
                raise RuntimeError("Unsupported CLIO capacity journal schema")
            if _resume_plan(store, config, journal_path, journal):
                return True
            if journal["config_sha256"] != _digest(text):
                if journal["config_sha256"] not in map(_digest, _before_clio_upgrades(text)):
                    logger.warning("reason=clio_core_capacity_config_edited config=%s", config)
                    return False
                # Only CLIO's own in-place upgrades differ: the config is still CLIO's.
                journal["config_sha256"] = _digest(text)
                _persist_journal(journal_path, journal)
                logger.warning("reason=clio_core_capacity_journal_rekeyed config=%s", config)
            # Recheck opt-out even after automatic growth created the journal.
            if not _growth_enabled(config):
                return False
            document = yaml.safe_load(text)
        else:
            document = _managed_config(config, text)
            if document is None:
                return False
            journal = {"schema": 1, "config_sha256": _digest(text)}
        needed = _additional_bytes(store, tag_id, name, payload_bytes)
        if needed <= 0:
            return False
        if not all(
            hasattr(store._client, name)
            for name in (
                "AsyncStatTargets",
                "AsyncGetCapacity",
                "AsyncGetTargetInfo",
            )
        ):
            raise RuntimeError("CLIO capacity recovery requires the updated Core capacity bindings")
        local = store._cte.PoolQuery.Local()
        refreshed = await_future(store._client.AsyncStatTargets(local), op_name="capacity_refresh")
        if refreshed.wait(0) != 0:
            raise RuntimeError("clio-core target capacity refresh failed")
        capacity = _future_result(store._client.AsyncGetCapacity(local), "capacity_inspect")
        module = document["compose"][1]
        tiers = module["storage"]
        remaining = [
            int(
                _future_result(
                    store._client.AsyncGetTargetInfo(
                        f"{tier['path']}_node0",
                        local,
                    ),
                    "capacity_target",
                ).remaining_space
            )
            for tier in tiers
        ]
        # max_bw admits the complete growth on one target, not on the sum of
        # several small free extents. A target with room makes code 12 an
        # unconfirmed placement/health failure, for which growth is inappropriate.
        if remaining and max(remaining) >= needed:
            return False
        logger.warning(
            "reason=clio_core_capacity_exhausted config=%s total=%d remaining=%d required=%d",
            config,
            capacity.total_capacity,
            capacity.remaining_capacity,
            needed,
        )
        total = sum(parse_capacity_bytes(str(tier["capacity_limit"])) for tier in tiers)
        if len(tiers) >= _MAX_TARGETS or total >= _MAX_TOTAL:
            raise RuntimeError(
                "CLIO managed storage growth limit reached; configure a larger store"
            )
        free = shutil.disk_usage(config.parent).free
        available = free - max(_RESERVE, free // 10)
        size = min(max(needed * 2, 2 * _GIB, free // 10), 50 * _GIB, _MAX_TOTAL - total)
        if size > available or size < needed:
            raise RuntimeError(
                "CLIO storage disk has insufficient free space for safe capacity growth"
            )
        index = len(tiers)
        target = config.parent / f"storage-extra-{index:03d}.bin"
        if target.exists() or Path(f"{target}_node0").exists():
            raise RuntimeError(f"Refusing to reuse an unregistered CLIO storage file: {target}")
        tiers.append({**tiers[0], "path": _cte_yaml_path(target), "capacity_limit": f"{size}B"})
        journal["pending"] = {
            "index": index,
            "path": _cte_yaml_path(target),
            "capacity": size,
            "config_text": yaml.safe_dump(document, sort_keys=False),
        }
        _persist_journal(journal_path, journal)
        return _resume_plan(store, config, journal_path, journal)


def _growth_enabled(config: Path) -> bool:
    """Whether CLIO grows this store: its own generated config, unless auto_expand is off.

    An explicit ``arc.cte.file_capacity`` is the size CLIO seeds the file tier with, not
    a ceiling: a full tier still refuses what the agent must store (take 1r: a 512 MB
    tier refused a 27.7 MB event chunk with code 12). Only ``arc.cte.auto_expand=false``
    keeps a fixed capacity.
    """
    return config == (_default_cte_dir() / "cte.yaml").resolve() and _auto_expand()


def _before_clio_upgrades(text: str) -> list[str]:
    """``text`` without each combination of CLIO's own in-place additions to its seed.

    The durability line (``ensure_seeded_config_durable``) and the indexer entry
    (``ensure_seeded_config_indexed``), each removed only when it is exactly what CLIO
    adds, so a person's edit is never mistaken for one.
    """
    without_line = text.replace(_PERSISTENCE_LINE, "", 1)
    variants = [without_line] if without_line != text else []
    marker = "  - mod_name: clio_cte_indexer\n"
    if marker in text:
        start = text.index(marker)
        log = re.search(r'index_log_path: "([^"]*)"', text[start:])
        if log and text[start:] == indexer_block(log.group(1)):
            variants += [text[:start], text[:start].replace(_PERSISTENCE_LINE, "", 1)]
    return variants


def rekey_managed_capacity(config: Path, before: str, after: str) -> None:
    """Keep CLIO's ownership of its config across an edit CLIO itself made.

    A journal recorded for ``before`` moves to ``after``; one recorded for anything
    else (a person's edit) is left alone, so that edit still opts out of growth.
    """
    journal_path = config.resolve().with_name("cte-capacity.json")
    if not journal_path.is_file():
        return
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal.get("config_sha256") == _digest(before) and "pending" not in journal:
        journal["config_sha256"] = _digest(after)
        _persist_journal(journal_path, journal)


def initialize_managed_capacity(config: Path) -> None:
    """Record ownership of a newly generated seed; future manual edits opt out."""
    config = config.resolve()
    if _growth_enabled(config):
        _persist_journal(
            config.with_name("cte-capacity.json"),
            {
                "schema": 1,
                "config_sha256": _digest(config.read_text(encoding="utf-8")),
            },
        )


#: Below this total a file store cannot hold a long conversation's events (take 1r and
#: W17: a 512 MB tier refused event chunks with codes 12 and 13). The seed rule's floor.
_UNDERSIZED_BYTES = 2 * _GIB
#: Undersized stores already reported this process, so a boot logs each once.
_WARNED_UNDERSIZED: set[Path] = set()


def _journal_owns(journal_path: Path, text: str) -> bool:
    """Whether CLIO's capacity journal still owns ``text`` (so growth will extend it)."""
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(journal, dict) or journal.get("schema") != 1:
        return False
    owned = {_digest(text), *map(_digest, _before_clio_upgrades(text))}
    pending = journal.get("pending")
    if isinstance(pending, dict) and isinstance(pending.get("config_text"), str):
        owned.add(_digest(pending["config_text"]))
    return journal.get("config_sha256") in owned


def _file_tier_bytes(text: str) -> int | None:
    """The summed ``capacity_limit`` of a config's file tiers, ``None`` when unreadable."""
    try:
        document = yaml.safe_load(text)
        total = 0
        for module in document["compose"]:
            for tier in (module.get("storage") or []) if isinstance(module, dict) else []:
                if str(tier.get("bdev_type", "")).strip().lower() == "file":
                    total += parse_capacity_bytes(str(tier["capacity_limit"]))
    except (KeyError, TypeError, ValueError, AttributeError, yaml.YAMLError):
        return None
    return total


def adopt_or_warn_undersized(config: Path) -> None:
    """At boot, bring CLIO's own store under growth, or name the fix for one it cannot grow.

    A store CLIO generated before it kept a capacity journal (or while an explicit
    ``arc.cte.file_capacity`` turned growth off) is adopted here, whatever capacity it was
    seeded with, so a full tier grows instead of refusing the agent's data. A store growth
    cannot reach (a hand-edited or custom config, an explicit ``arc.cte.dir``, or
    ``arc.cte.auto_expand=false``) whose file tiers total less than the seed floor is
    logged once per boot with the exact edit that raises it; it is never rewritten.
    """
    config = config.resolve()
    journal_path = config.with_name("cte-capacity.json")
    try:
        text = config.read_text(encoding="utf-8")
    except OSError:
        return
    total = _file_tier_bytes(text)
    if _growth_enabled(config):
        if journal_path.is_file() and _journal_owns(journal_path, text):
            return
        if not journal_path.is_file() and _managed_config(config, text) is not None:
            _persist_journal(journal_path, {"schema": 1, "config_sha256": _digest(text)})
            logger.warning(
                "reason=clio_core_store_adopted config=%s file_capacity_bytes=%s: CLIO's "
                "generated store had no capacity journal; it now grows when full",
                config,
                total,
            )
            return
    if total is None or total >= _UNDERSIZED_BYTES or config in _WARNED_UNDERSIZED:
        return
    _WARNED_UNDERSIZED.add(config)
    from clio_agent.arc.clio_core_file_capacity import seeded_file_capacity  # noqa: PLC0415

    seed = seeded_file_capacity(config.parent)
    growth = "off (arc.cte.auto_expand=false)" if not _auto_expand() else "unavailable"
    logger.warning(
        "reason=clio_core_store_undersized config=%s file_capacity_bytes=%d growth=%s: this "
        "store refuses agent data once full (clio-core code 12/13). Fix: in %s set the file "
        'tier\'s capacity_limit to "%s" (its storage file and data are kept), then restart '
        "clio-core%s",
        config,
        total,
        growth,
        config,
        seed,
        "; or set arc.cte.auto_expand: true" if not _auto_expand() else "",
    )


def _auto_expand() -> bool:
    return conf.resolve(
        "arc.cte.auto_expand", env="CLIO_ARC_CTE_AUTO_EXPAND", default=True, cast=conf.as_bool
    )
