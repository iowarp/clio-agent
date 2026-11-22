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
from clio_agent.arc.clio_core_durability import _PERSISTENCE_LINE, DURABILITY_LINE
from clio_agent.arc.clio_core_indexer import indexer_block

logger = logging.getLogger(__name__)
_GIB = 1 << 30
_RESERVE = _GIB
_MAX_TARGETS = 8
_MAX_TOTAL = 500 * _GIB


#: clio-core's ``persistence_level`` labels and their native values (core_bindings.cc).
_PERSISTENCE_LEVELS = {"volatile": 0, "temporary": 1, "long_term": 2}


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
    """Recognize the generated default; explicit config/capacity choices opt out."""
    explicit = conf.resolve(
        "arc.cte.file_capacity", env="CLIO_ARC_CTE_FILE_CAPACITY", default="", cast=conf.as_str
    )
    enabled = conf.resolve(
        "arc.cte.auto_expand", env="CLIO_ARC_CTE_AUTO_EXPAND", default=True, cast=conf.as_bool
    )
    if explicit or not enabled or config != (_default_cte_dir() / "cte.yaml").resolve():
        return None
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        return None
    try:
        ram, module, *indexer = document["compose"]
        (tier,) = module["storage"]
        capacity = parse_capacity_bytes(tier["capacity_limit"])
        # Older generated configs have no journal. Adopt only the unchanged seed
        # shape, owned paths and original bounded seed range. Custom topologies,
        # capacities, paths, limits or policies are never rewritten.
        if not 2 * _GIB <= capacity <= 50 * _GIB or capacity % _GIB:
            return None
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
    same = _without_stated_durability(document) == _without_stated_durability(
        yaml.safe_load(expected)
    )
    return document if same else None


def _without_stated_durability(document: dict[str, Any]) -> dict[str, Any]:
    """``document`` with the core's ``durability: persistent`` taken out.

    The seed states it since iowarp-core 2.5 (a seed from before has the same, inferred,
    durability), so both shapes are CLIO's unchanged seed.
    """
    compose = [dict(m) if isinstance(m, dict) else m for m in document.get("compose", [])]
    for module in compose:
        if isinstance(module, dict) and module.get("durability") == "persistent":
            del module["durability"]
    return {**document, "compose": compose}


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


def _planned_persistence(plan: dict[str, Any]) -> int:
    """Native persistence level of the planned tier (a copy of the config's first tier).

    iowarp-core 2.5 resolves an unlabelled target only against the devices in the
    config the daemon loaded at start, so a target added later counts as volatile and
    a persistent core refuses it (rc 16). The level is passed explicitly instead.
    """
    tiers = yaml.safe_load(plan["config_text"])["compose"][1]["storage"]
    label = str(tiers[int(plan["index"])].get("persistence_level", "temporary"))
    if label not in _PERSISTENCE_LEVELS:
        raise ValueError(f"unknown clio-core persistence_level {label!r} in the growth plan")
    return _PERSISTENCE_LEVELS[label]


def _register(store: Any, plan: dict[str, Any]) -> None:
    index = int(plan["index"])
    future = await_future(
        store._client.AsyncRegisterTarget(
            f"{plan['path']}_node0",
            store._cte.BdevType.kFile,
            int(plan["capacity"]),
            store._cte.PoolQuery.Local(),
            store._cte.PoolId(512 + index, 1),
            persistence_level=_planned_persistence(plan),
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
    explicit = conf.resolve(
        "arc.cte.file_capacity", env="CLIO_ARC_CTE_FILE_CAPACITY", default="", cast=conf.as_str
    )
    return (
        not explicit
        and config == (_default_cte_dir() / "cte.yaml").resolve()
        and conf.resolve(
            "arc.cte.auto_expand", env="CLIO_ARC_CTE_AUTO_EXPAND", default=True, cast=conf.as_bool
        )
    )


def _before_clio_upgrades(text: str) -> list[str]:
    """``text`` without each combination of CLIO's own in-place additions to its seed.

    The durability lines (``ensure_seeded_config_durable``: the file tier's
    ``persistence_level`` and, for a seed with a RAM data tier, the core's
    ``durability: persistent``) and the indexer entry
    (``ensure_seeded_config_indexed``), each removed only when it is exactly what CLIO
    adds, so a person's edit is never mistaken for one.
    """

    def without_durability(base: str) -> list[str]:
        # Every combination of the two durability lines CLIO may have added.
        out = []
        for line in (_PERSISTENCE_LINE, DURABILITY_LINE):
            out += [v.replace(line, "", 1) for v in [base, *out] if line in v]
        return out

    variants = without_durability(text)
    marker = "  - mod_name: clio_cte_indexer\n"
    if marker in text:
        start = text.index(marker)
        log = re.search(r'index_log_path: "([^"]*)"', text[start:])
        if log and text[start:] == indexer_block(log.group(1)):
            variants += [text[:start], *without_durability(text[:start])]
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
