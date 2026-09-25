"""One-time boot migration: rewrite persisted ``codex`` provider config to ``chatgpt``.

The direct ChatGPT provider (S1) REPLACES the deleted Codex SDK provider
outright -- no flag, no parallel path. A machine that had
``lm.provider: codex`` (or ``lm.codex_transport``) in its shared config file
would otherwise keep naming a provider kind clio no longer registers. This
module rewrites that ONE persisted source of truth for "which provider clio
talks to" (the nested ``config.yaml`` the ``conf`` file layer reads -- see
``clio_agent.conf``'s workspace/user precedence) at boot, before the first
``load_config_from_env()`` call, and records a typed reason either way.

Scope, stated honestly: this migrates the shared provider CONFIG file only.
Per-session model refs and Agent Blueprint provider refs that name ``codex``
are a broader migration across stores this slice does not touch; a session
or agent still naming ``codex`` falls back to the provider catalog's normal
"unknown provider" handling rather than being silently rewritten.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from clio_agent import paths

logger = logging.getLogger(__name__)

__all__ = [
    "MIGRATION_REASONS",
    "config_file_paths",
    "migrate_codex_provider_configs",
]

#: Typed per-file outcomes (no-silent-fallback ground rule): every migration
#: attempt records one of these, never a bare pass/fail.
MIGRATION_REASONS = frozenset(
    {
        "missing",  # the file does not exist -- nothing to migrate
        "unreadable",  # present but not parseable YAML / not a mapping
        "no_codex_reference",  # present, valid, names no codex provider
        "migrated",  # rewritten codex -> chatgpt
        "write_failed",  # rewrite attempted but the atomic replace failed
    }
)

_WORKSPACE_CONFIG_RELPATH = (".clio", "config.yaml")


def config_file_paths() -> tuple[Path, ...]:
    """The nested ``config.yaml`` files the ``conf`` file layer reads, in precedence order.

    Mirrors ``clio_agent.conf.ConfigStore``'s own resolution (workspace
    ``<cwd>/.clio/config.yaml`` deep-merged over user
    ``<config>/clio-agent/config.yaml``, see ``clio_agent.paths.user_config_dir``)
    rather than hardcoding a single path, so the migration touches the SAME
    files a real boot would read.
    """

    return (
        paths.user_config_dir() / "config.yaml",
        Path.cwd().joinpath(*_WORKSPACE_CONFIG_RELPATH),
    )


def _migrate_one_file(path: Path) -> str:
    """Rewrite ``lm.provider: codex`` (and its transport) in one config.yaml.

    Returns one of :data:`MIGRATION_REASONS`. Never raises: any failure short
    of a successful rewrite is reported as a typed reason string.
    """

    if not path.is_file():
        return "missing"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("chatgpt provider migration: unreadable path=%s reason=%s", path, exc)
        return "unreadable"
    if raw is None:
        return "no_codex_reference"
    if not isinstance(raw, dict):
        return "unreadable"
    lm_section = raw.get("lm")
    if not isinstance(lm_section, dict) or lm_section.get("provider") != "codex":
        return "no_codex_reference"

    lm_section = dict(lm_section)
    lm_section["provider"] = "chatgpt"
    if "codex_transport" in lm_section:
        old_transport = lm_section.pop("codex_transport")
        lm_section["chatgpt_transport"] = "sse" if old_transport == "sse" else "websocket"
    raw = {**raw, "lm": lm_section}

    try:
        tmp = path.with_suffix(f"{path.suffix}.tmp")
        tmp.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        logger.warning("chatgpt provider migration: write_failed path=%s reason=%s", path, exc)
        return "write_failed"
    return "migrated"


def migrate_codex_provider_configs() -> dict[str, Any]:
    """Run the one-time ``codex`` -> ``chatgpt`` config migration across every config file.

    Idempotent: once a file's ``lm.provider`` reads ``chatgpt``, a later run
    reports ``no_codex_reference`` for it and touches nothing. Safe to call on
    every boot (RULE 2: never break baseline) -- a failure on one file never
    raises and never blocks the others.

    Returns:
        ``{"migrated": bool, "files": {path: reason}}`` -- ``migrated`` is
        ``True`` when at least one file was actually rewritten.
    """

    results: dict[str, str] = {}
    for path in config_file_paths():
        try:
            results[str(path)] = _migrate_one_file(path)
        except Exception as exc:  # noqa: BLE001 - a migration bug must never break boot
            logger.warning("chatgpt provider migration: crashed path=%s reason=%r", path, exc)
            results[str(path)] = "write_failed"
    migrated_any = "migrated" in results.values()
    if migrated_any:
        logger.warning(
            "chatgpt provider migration: rewrote codex -> chatgpt reason=codex_provider_migrated files=%s",
            results,
        )
    return {"migrated": migrated_any, "files": results}
