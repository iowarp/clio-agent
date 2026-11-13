"""Connector entries: ``catalogs/connectors/*.yaml``, validated (CN1, CN2, CN11).

Each file is a list of entries checked against ``connector.schema.json`` beside it,
then against the placement rules the schema cannot say:

* every ``${need.<key>}`` names a need of the entry, and a sign-in is never one;
* a secret is substituted only into headers or the environment, never into the
  URL, the command or its arguments (CN2), so it never reaches a process list or
  a URL that is logged;
* a sign-in need belongs to a URL server, at most one per entry;
* a secret or a sign-in has no default and is never ``agent_settable`` (CN6);
* a logo is an SVG file beside the entry.

An invalid entry is left out and reported as a typed diagnostic; the others load.
The shipped folder is ``catalogs/connectors`` (in a wheel, ``clio_agent/_catalogs/
connectors``). More folders, such as test fixtures, are added with the
``connectors.catalog_dirs`` setting (``CLIO_CONNECTOR_CATALOG_DIRS``, separated
like ``PATH``); a later folder cannot replace an entry of an earlier one.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

NeedKind = Literal["address", "sign-in", "secret", "value"]
NEED_KINDS: tuple[NeedKind, ...] = ("address", "sign-in", "secret", "value")
PLACEHOLDER = re.compile(r"\$\{need\.([a-z][a-z0-9_]*)\}")
SCHEMA_FILE = "connector.schema.json"

#: Typed diagnostic codes.
ENTRY_FILE_UNREADABLE = "connector_file_unreadable"
ENTRY_INVALID = "connector_entry_invalid"
ENTRY_DUPLICATE = "connector_entry_duplicate"


@dataclass(frozen=True)
class Need:
    """One thing a connector needs from the person (CN2)."""

    key: str
    kind: NeedKind
    label: str
    help: str = ""
    required: bool = True
    default: str | None = None
    agent_settable: bool = False
    options: tuple[str, ...] = ()

    @property
    def substituted(self) -> bool:
        """Whether the value is written into the server declaration (all but sign-in)."""
        return self.kind != "sign-in"


@dataclass(frozen=True)
class Entry:
    """One connector a person can enable."""

    id: str
    name: str
    description: str
    server: Mapping[str, Any]
    needs: tuple[Need, ...] = ()
    logo: str = ""
    plugin: str = ""
    source: Literal["catalog", "custom"] = "catalog"
    folder: str = ""

    def need(self, key: str) -> Need | None:
        return next((need for need in self.needs if need.key == key), None)

    @property
    def is_url(self) -> bool:
        return "url" in self.server

    @property
    def logo_path(self) -> Path | None:
        return Path(self.folder) / self.logo if self.logo and self.folder else None

    def to_dict(self) -> dict[str, Any]:
        """The entry as data (a custom connector is stored this way)."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "server": dict(self.server),
            "needs": [
                {
                    key: value
                    for key, value in {
                        "key": need.key,
                        "kind": need.kind,
                        "label": need.label,
                        "help": need.help or None,
                        "required": need.required,
                        "default": need.default,
                        "agent_settable": need.agent_settable or None,
                        "options": list(need.options) or None,
                    }.items()
                    if value is not None
                }
                for need in self.needs
            ],
            **({"plugin": self.plugin} if self.plugin else {}),
        }


@dataclass(frozen=True)
class Diagnostic:
    """Why one file or entry did not load."""

    code: str
    message: str
    file: str = ""
    entry: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "file": self.file, "entry": self.entry}


@dataclass
class Catalog:
    """Every valid entry by id, in load order, with what was left out."""

    entries: dict[str, Entry] = field(default_factory=dict)
    diagnostics: list[Diagnostic] = field(default_factory=list)


def shipped_dir() -> Path:
    """The connector catalog shipped with CLIO (the wheel copy, else the checkout's)."""

    package = Path(__file__).resolve().parents[1] / "_catalogs" / "connectors"
    if package.is_dir():
        return package
    return Path(__file__).resolve().parents[3] / "catalogs" / "connectors"


def catalog_dirs() -> list[Path]:
    """The shipped folder, then each folder named by ``connectors.catalog_dirs``."""

    from clio_agent import conf  # noqa: PLC0415

    raw = conf.resolve(
        "connectors.catalog_dirs",
        env="CLIO_CONNECTOR_CATALOG_DIRS",
        default="",
        cast=conf.as_str,
    )
    extra = [Path(part).expanduser() for part in str(raw or "").split(os.pathsep) if part.strip()]
    return [shipped_dir(), *extra]


@lru_cache(maxsize=1)
def _validator() -> Any:
    import jsonschema  # noqa: PLC0415

    schema = json.loads((shipped_dir() / SCHEMA_FILE).read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def _strings(server: Mapping[str, Any]) -> Iterable[tuple[str, str]]:
    """Every (place, text) of a server declaration where a placeholder may appear."""

    for key in ("url", "command"):
        if isinstance(server.get(key), str):
            yield key, server[key]
    for arg in server.get("args") or ():
        yield "args", str(arg)
    for place in ("headers", "env"):
        for value in (server.get(place) or {}).values():
            yield place, str(value)


def placement_errors(raw: Mapping[str, Any]) -> list[str]:
    """The rules the schema cannot express (module docstring)."""

    errors: list[str] = []
    needs = {str(row["key"]): row for row in raw.get("needs") or ()}
    if len(needs) != len(raw.get("needs") or ()):
        errors.append("need keys must be unique")
    server = raw.get("server") or {}
    for place, text in _strings(server):
        for key in PLACEHOLDER.findall(text):
            need = needs.get(key)
            if need is None:
                errors.append(f"${{need.{key}}} in {place} names no need")
            elif need["kind"] == "sign-in":
                errors.append(f"sign-in need {key!r} cannot be substituted")
            elif need["kind"] == "secret" and place not in {"headers", "env"}:
                errors.append(f"secret {key!r} may only be used in headers or env, not {place}")
    sign_ins = [row for row in needs.values() if row["kind"] == "sign-in"]
    if sign_ins and "url" not in server:
        errors.append("a sign-in need needs a URL server")
    if len(sign_ins) > 1:
        errors.append("at most one sign-in need")
    for key, row in needs.items():
        if row["kind"] in {"secret", "sign-in"}:
            if "default" in row:
                errors.append(f"{row['kind']} need {key!r} cannot have a default")
            if row.get("agent_settable"):
                errors.append(f"{row['kind']} need {key!r} cannot be agent_settable")
        if row.get("options") and row["kind"] != "value":
            errors.append(f"only a value need offers options ({key!r})")
    return errors


def entry_from_raw(
    raw: Mapping[str, Any], *, folder: str, source: Literal["catalog", "custom"] = "catalog"
) -> Entry:
    """Build an :class:`Entry` from validated data."""

    return Entry(
        id=str(raw["id"]),
        name=str(raw["name"]),
        description=str(raw["description"]),
        server=dict(raw["server"]),
        needs=tuple(
            Need(
                key=str(row["key"]),
                kind=row["kind"],
                label=str(row["label"]),
                help=str(row.get("help") or ""),
                required=bool(row.get("required", True)),
                default=str(row["default"]) if row.get("default") is not None else None,
                agent_settable=bool(row.get("agent_settable", False)),
                options=tuple(str(value) for value in row.get("options") or ()),
            )
            for row in raw.get("needs") or ()
        ),
        logo=str(raw.get("logo") or ""),
        plugin=str(raw.get("plugin") or ""),
        source=source,
        folder=folder,
    )


def validate_entry(raw: Any, *, folder: Path | None) -> list[str]:
    """Every reason ``raw`` is not a valid entry (empty when it is)."""

    errors = [
        f"{'/'.join(str(part) for part in error.absolute_path) or 'entry'}: {error.message}"
        for error in _validator().iter_errors([raw])
    ]
    if errors:
        return errors
    errors = placement_errors(raw)
    logo = raw.get("logo")
    if logo and folder is not None and not (folder / str(logo)).is_file():
        errors.append(f"logo {logo!r} is not beside the entry")
    return errors


def load_folder(folder: Path, catalog: Catalog) -> None:
    """Add every valid entry of ``folder``'s ``*.yaml`` files to ``catalog``."""

    if not folder.is_dir():
        return
    for path in sorted(folder.glob("*.yaml")):
        try:
            rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        except (OSError, yaml.YAMLError) as exc:
            catalog.diagnostics.append(Diagnostic(ENTRY_FILE_UNREADABLE, str(exc), str(path)))
            continue
        if not isinstance(rows, list):
            catalog.diagnostics.append(
                Diagnostic(ENTRY_FILE_UNREADABLE, "a connector file is a list", str(path))
            )
            continue
        for raw in rows:
            entry_id = str(raw.get("id") or "") if isinstance(raw, Mapping) else ""
            errors = validate_entry(raw, folder=folder)
            if errors:
                catalog.diagnostics.append(
                    Diagnostic(ENTRY_INVALID, "; ".join(errors), str(path), entry_id)
                )
                continue
            if entry_id in catalog.entries:
                catalog.diagnostics.append(
                    Diagnostic(
                        ENTRY_DUPLICATE, "an earlier folder has this id", str(path), entry_id
                    )
                )
                continue
            catalog.entries[entry_id] = entry_from_raw(raw, folder=str(folder))


def _stamp(dirs: list[Path]) -> tuple[Any, ...]:
    stamp: list[Any] = []
    for folder in dirs:
        if folder.is_dir():
            stamp.extend((str(p), p.stat().st_mtime_ns) for p in sorted(folder.glob("*")))
    return tuple(stamp)


_CACHE: dict[str, Any] = {}


def load_catalog(dirs: list[Path] | None = None) -> Catalog:
    """Load the connector catalog (cached until a file in its folders changes)."""

    folders = dirs if dirs is not None else catalog_dirs()
    stamp = (tuple(str(folder) for folder in folders), _stamp(folders))
    if _CACHE.get("stamp") == stamp:
        return _CACHE["catalog"]
    catalog = Catalog()
    for folder in folders:
        load_folder(folder, catalog)
    _CACHE.update(stamp=stamp, catalog=catalog)
    return catalog
