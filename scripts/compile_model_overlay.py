#!/usr/bin/env python3
"""Compile ``catalogs/models/*.yaml`` into ``catalogs/model-overlay.json``.

The model overlay (model-capabilities brief Part 8) is authored as one YAML
file per model family under ``catalogs/models/``, each a list of clio-coder-
shaped knowledge-base entries (``family``, ``matchPatterns``, ``capabilities``,
``quirks``). People edit the YAML; code only ever reads the compiled JSON
(``catalogs/model-overlay.json``), fetched by
:mod:`clio_agent.providers.model_discovery.model_overlay_catalog` the same way
``catalogs/claude-code-models.json`` is (brief Part 8.1).

This script has three jobs, all enforced in CI (the ``check`` job in
``.github/workflows/ci.yml``):

1. **Validate** every ``catalogs/models/*.yaml`` file against
   ``catalogs/models/overlay.schema.json`` (Draft 2020-12), and reject a
   duplicate ``family`` name across files (one family, one file -- brief Part
   8.2).
2. **Compile** the validated entries into one deterministic, sorted JSON
   document. Same input -> byte-identical output, always -- this is what lets
   ``--check`` catch a stale commit (someone hand-edited a YAML file and forgot
   to re-run this script).
3. **Validate the two adjacent hand-maintained catalogs**
   (``catalogs/claude-code-models.json`` against
   ``catalogs/claude-code-models.schema.json``, and -- only if it exists yet --
   ``catalogs/codex-models.json`` against ``catalogs/codex-models.schema.json``)
   since neither had a schema check before this script (brief Part 8, deliverable
   6). Skipped, not failed, when a catalog file is absent (the codex catalog is a
   different slice's deliverable and may not have landed yet).

Usage::

    uv run python scripts/compile_model_overlay.py          # (re)writes the compiled JSON
    uv run python scripts/compile_model_overlay.py --check  # CI mode: fail if stale/invalid, write nothing
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = REPO_ROOT / "catalogs" / "models"
OVERLAY_SCHEMA_PATH = MODELS_DIR / "overlay.schema.json"
COMPILED_OVERLAY_PATH = REPO_ROOT / "catalogs" / "model-overlay.json"

#: Packaged read-only cold-start copy (brief Part 8.1: "the committed file as
#: the cold-start bundled source"), mirroring the existing packaged-seed
#: pattern for the model-limits DB (``providers/handshake/sources/data/
#: model_limits.json``) rather than inventing a new one. This lives INSIDE
#: ``src/clio_agent`` so it ships in the wheel/sdist the same way that seed
#: does; ``catalogs/model-overlay.json`` stays the canonical, publicly
#: fetchable copy (raw.githubusercontent.com serves the repo root, not
#: package internals). Kept byte-identical to the canonical copy by this
#: script -- never hand-edited.
BUNDLED_OVERLAY_PATH = (
    REPO_ROOT
    / "src"
    / "clio_agent"
    / "providers"
    / "model_discovery"
    / "data"
    / "model-overlay.json"
)

CLAUDE_CODE_CATALOG_PATH = REPO_ROOT / "catalogs" / "claude-code-models.json"
CLAUDE_CODE_SCHEMA_PATH = REPO_ROOT / "catalogs" / "claude-code-models.schema.json"

#: Brief Part 8, deliverable 6: "If catalogs/codex-models.json from S1 exists on
#: develop when you merge, validate it too." Neither the catalog nor its schema
#: exist in this tree yet -- both checks below no-op (never fail) until S1
#: lands them, at which point they behave exactly like the Claude Code pair.
CODEX_CATALOG_PATH = REPO_ROOT / "catalogs" / "codex-models.json"
CODEX_SCHEMA_PATH = REPO_ROOT / "catalogs" / "codex-models.schema.json"

SCHEMA_VERSION = 1


class OverlayCompileError(RuntimeError):
    """One or more catalog files failed validation or compilation."""


def _load_schema(path: Path) -> dict[str, Any]:
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return schema


def _family_files() -> list[Path]:
    if not MODELS_DIR.is_dir():
        raise OverlayCompileError(f"{MODELS_DIR} does not exist")
    return sorted(p for p in MODELS_DIR.glob("*.yaml") if p.is_file())


def load_and_validate_entries() -> list[dict[str, Any]]:
    """Parse + schema-validate every ``catalogs/models/*.yaml`` file.

    Returns:
        Every entry across every file, in a stable (family-sorted) order.

    Raises:
        OverlayCompileError: A file failed to parse, failed schema validation,
            or a ``family`` name repeats across two files.
    """

    schema = _load_schema(OVERLAY_SCHEMA_PATH)
    validator = Draft202012Validator(schema)
    files = _family_files()
    if not files:
        raise OverlayCompileError(f"no *.yaml files found under {MODELS_DIR}")

    entries: list[dict[str, Any]] = []
    family_owner: dict[str, str] = {}
    problems: list[str] = []

    for path in files:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(f"{path.name}: invalid YAML: {exc}")
            continue
        if not isinstance(raw, list) or not raw:
            problems.append(f"{path.name}: must be a non-empty YAML list of entries")
            continue
        for error in validator.iter_errors(raw):
            pointer = "/".join(str(p) for p in error.absolute_path) or "<entry>"
            problems.append(f"{path.name} at {pointer}: {error.message}")
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict) or "family" not in entry:
                continue
            family = entry["family"]
            if family in family_owner and family_owner[family] != path.name:
                problems.append(
                    f"{path.name}[{index}]: family {family!r} is already defined in "
                    f"{family_owner[family]} -- one family, one file (brief Part 8.2)"
                )
            else:
                family_owner[family] = path.name
            entries.append(entry)

    if problems:
        raise OverlayCompileError(
            "model overlay validation failed:\n" + "\n".join(f"  - {p}" for p in problems)
        )
    return entries


def compile_overlay(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the deterministic compiled document from validated entries.

    Sorted by ``family`` (never by filesystem/YAML load order) so the output
    is stable regardless of directory-listing order across platforms, and
    ``json.dumps(..., sort_keys=True)`` at serialization time makes every
    nested mapping's key order deterministic too. No timestamp is embedded --
    a generated-at field would make every recompilation "stale" against the
    committed copy even when nothing actually changed.
    """

    sorted_entries = sorted(entries, key=lambda e: e["family"])
    return {"schema_version": SCHEMA_VERSION, "entries": sorted_entries}


def render(document: dict[str, Any]) -> str:
    """Deterministic, diff-friendly JSON text for the compiled document."""

    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _validate_catalog_if_present(catalog_path: Path, schema_path: Path, *, label: str) -> list[str]:
    """Validate one hand-maintained catalog JSON file against its schema.

    Returns a list of problem strings (empty means "valid" or "not present
    yet" -- the caller decides which). A catalog with no schema file yet is
    reported once as a problem when the CATALOG itself exists (an unvalidated
    committed catalog is exactly the gap this script closes); a catalog that
    does not exist at all is silently skipped (brief: "only if it exists").
    """

    if not catalog_path.exists():
        return []
    if not schema_path.exists():
        return [f"{catalog_path.name} exists but {schema_path.name} does not (add a schema)"]
    try:
        schema = _load_schema(schema_path)
    except Exception as exc:  # noqa: BLE001 - reported to the caller, not raised
        return [f"{label} schema {schema_path.name} is invalid: {exc}"]
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"{label} {catalog_path.name} is not valid JSON: {exc}"]
    validator = Draft202012Validator(schema)
    problems = []
    for error in validator.iter_errors(data):
        pointer = "/".join(str(p) for p in error.absolute_path) or "<root>"
        problems.append(f"{label} {catalog_path.name} at {pointer}: {error.message}")
    return problems


def validate_adjacent_catalogs() -> list[str]:
    """Schema-validate the Claude Code catalog, and the codex one if it exists."""

    problems = _validate_catalog_if_present(
        CLAUDE_CODE_CATALOG_PATH, CLAUDE_CODE_SCHEMA_PATH, label="claude-code catalog"
    )
    problems += _validate_catalog_if_present(
        CODEX_CATALOG_PATH, CODEX_SCHEMA_PATH, label="codex catalog"
    )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate and recompile in memory; fail if the committed JSON is stale or invalid. Writes nothing.",
    )
    args = parser.parse_args(argv)

    try:
        entries = load_and_validate_entries()
    except OverlayCompileError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    catalog_problems = validate_adjacent_catalogs()
    if catalog_problems:
        print("catalog schema validation failed:", file=sys.stderr)
        for problem in catalog_problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    document = compile_overlay(entries)
    rendered = render(document)

    if args.check:
        stale: list[str] = []
        for path in (COMPILED_OVERLAY_PATH, BUNDLED_OVERLAY_PATH):
            if not path.exists():
                stale.append(f"{path} does not exist")
            elif path.read_text(encoding="utf-8") != rendered:
                stale.append(f"{path} is stale relative to catalogs/models/*.yaml")
        if stale:
            for message in stale:
                print(message, file=sys.stderr)
            print(
                "run 'uv run python scripts/compile_model_overlay.py' and commit the result",
                file=sys.stderr,
            )
            return 1
        print(
            f"OK: {COMPILED_OVERLAY_PATH} and its packaged copy are up to date "
            f"({len(entries)} entries)."
        )
        return 0

    COMPILED_OVERLAY_PATH.write_text(rendered, encoding="utf-8")
    BUNDLED_OVERLAY_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUNDLED_OVERLAY_PATH.write_text(rendered, encoding="utf-8")
    print(f"wrote {COMPILED_OVERLAY_PATH} and {BUNDLED_OVERLAY_PATH} ({len(entries)} entries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
