"""Installed marketplaces, plugin records, frozen agent specs and user agents.

Stage 1 of resolution happens here: installing a marketplace snapshots its
files, loads every plugin into a record, imports each agent class once and
stores its frozen spec with the checksum. Sessions never read marketplace
sources directly; they read this store.

Trust (``docs/design/sdk/plugins-and-composition.md``, "External plugins and trust"):

* plugins that ship inside an installed Python package (``core``,
  ``orchestration``) and the shipped default marketplace are trusted;
* any other git repository or external marketplace needs the explicit trust
  step before its executable parts load or run;
* an update that changes executable parts (server commands, Python tools,
  hooks, flows) is held as pending until the trust step runs again, while an
  update that changes only text (skills, fragments) applies to the next session.

Layout under ``<config>/plugins/``::

    state.json                     marketplaces, active and pending records, specs, trust
    snapshots/<market>/<digest>/   immutable copies the records point into
    sources/                       git fetch cache keyed by (url, commit)
    user-agents/<id>.json          parent plus delta, saved from the + menu
    resolved/<digest>.json         pinned session resolutions
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock

from clio_agent.plugins.claude_code import import_claude_plugin
from clio_agent.plugins.loader import PluginLoadError, load_plugin, tree_digests
from clio_agent.plugins.manifest import within
from clio_agent.plugins.marketplace import (
    GitPin,
    MarketplaceContents,
    read_claude_marketplace,
    read_marketplace,
)
from clio_agent.plugins.records import Diagnostic, PluginRecord
from clio_agent.plugins.sources import SourceError, fetch
from clio_agent.sdk.agent import AgentSpec
from clio_agent.sdk.versions import PluginRef, valid_plugin_id

BUILTIN_ROOT = Path(__file__).resolve().parent / "builtin"
BUILTIN_MARKETPLACE = "clio-builtin"
USER_MARKETPLACE = "user"
STATE_VERSION = 1


class StoreError(RuntimeError):
    """An install, trust, update or user-agent operation was refused."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class UserAgent:
    """A user agent: a parent agent plus a delta, never a flattened list."""

    id: str
    name: str
    extends: str
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "extends": self.extends,
            "add": list(self.add),
            "remove": list(self.remove),
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "UserAgent":
        return cls(
            id=str(data["id"]),
            name=str(data.get("name") or data["id"]),
            extends=str(data["extends"]),
            add=tuple(str(v) for v in data.get("add") or ()),
            remove=tuple(str(v) for v in data.get("remove") or ()),
            description=str(data.get("description") or ""),
        )


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _copy_tree(source: Path, target: Path) -> None:
    """Copy ``source`` to the content-addressed ``target``, once, however many install it.

    Each copy stages in its own folder, so concurrent installs of the same content never
    share one; the first to finish names it, and the others find it there and drop theirs.
    """

    if target.exists():
        return
    staging = target.with_name(f"{target.name}.partial-{uuid.uuid4().hex[:12]}")
    shutil.copytree(
        source,
        staging,
        symlinks=False,
        ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".venv", "node_modules"),
    )
    try:
        staging.rename(target)
    except OSError:
        if not target.is_dir():
            shutil.rmtree(staging, ignore_errors=True)
            raise
        # Another install of the same content finished first: its copy is identical.
        shutil.rmtree(staging, ignore_errors=True)


def builtin_records() -> list[PluginRecord]:
    """Plugins that ship inside clio-agent itself (trusted like any pip package)."""

    records = []
    for child in sorted(BUILTIN_ROOT.iterdir()) if BUILTIN_ROOT.is_dir() else []:
        if (child / "plugin.json").is_file():
            records.append(load_plugin(child, marketplace=BUILTIN_MARKETPLACE, trusted=True))
    return records


class PluginStore:
    """The persistent install state for one CLIO configuration directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = root / "state.json"
        self.lock = FileLock(str(root / "state.lock"))

    # ------------------------------------------------------------------ state
    def _empty(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            "marketplaces": {},
            "plugins": {},
            "pending": {},
            "agents": {},
        }

    def read_state(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return self._empty()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError("plugin_state_unreadable", f"{self.state_path}: {exc}") from exc
        if data.get("version") != STATE_VERSION:
            raise StoreError(
                "plugin_state_version", f"{self.state_path}: unsupported state version"
            )
        return data

    def _write_state(self, state: dict[str, Any]) -> None:
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.state_path)

    # ---------------------------------------------------------------- reading
    def marketplaces(self) -> dict[str, dict[str, Any]]:
        return dict(self.read_state()["marketplaces"])

    def records(self) -> list[PluginRecord]:
        """Every usable plugin record: built-ins first, then installed ones."""

        state = self.read_state()
        installed = [PluginRecord.from_dict(row) for row in state["plugins"].values()]
        return [*builtin_records(), *installed]

    def pending(self) -> list[PluginRecord]:
        return [PluginRecord.from_dict(row) for row in self.read_state()["pending"].values()]

    def trusted(self, record: PluginRecord) -> bool:
        if record.marketplace == BUILTIN_MARKETPLACE:
            return True
        market = self.read_state()["marketplaces"].get(record.marketplace) or {}
        if not market.get("trusted"):
            return False
        return (market.get("trusted_digests") or {}).get(record.id) == record.executable_digest

    def agent_specs(self) -> dict[str, AgentSpec]:
        state = self.read_state()
        return {key: AgentSpec.from_dict(value) for key, value in state["agents"].items()}

    def user_agents(self) -> dict[str, UserAgent]:
        base = self.root / "user-agents"
        out: dict[str, UserAgent] = {}
        for path in sorted(base.glob("*.json")) if base.is_dir() else []:
            try:
                agent = UserAgent.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, KeyError):
                continue
            out[f"{USER_MARKETPLACE}/{agent.id}"] = agent
        return out

    # ---------------------------------------------------------------- install
    def _snapshot(self, market: str, source: Path) -> Path:
        exec_digest, text_digest = tree_digests(source)
        digest = hashlib.sha256(f"{exec_digest}{text_digest}".encode()).hexdigest()[:16]
        target = self.root / "snapshots" / market / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        _copy_tree(source, target)
        return target

    def _read(self, path: Path, *, trusted: bool, name: str = "") -> MarketplaceContents:
        try:
            return read_marketplace(path, trusted=trusted, name_hint=name)
        except (PluginLoadError, OSError, json.JSONDecodeError) as exc:
            raise StoreError("marketplace_invalid", str(exc)) from exc

    def install(
        self,
        source: str,
        *,
        ref: str = "",
        commit: str = "",
        name: str = "",
        default: bool = False,
        parent: str = "",
    ) -> dict[str, Any]:
        """Install a marketplace (or a single plugin). Returns a summary.

        Only the shipped default marketplace is trusted at install; every other
        source waits for :meth:`trust`.
        """

        try:
            # A federated child's source is named by its parent's marketplace file.
            fetched = fetch(
                source,
                cache=self.root / "sources",
                ref=ref,
                commit=commit,
                declared=bool(parent),
            )
        except SourceError as exc:
            raise StoreError("source_unavailable", str(exc)) from exc
        probe = self._read(fetched.path, trusted=False, name=name)
        market = name or probe.name
        if not valid_plugin_id(market):
            raise StoreError(
                "marketplace_name_invalid", f"marketplace name {market!r} is not a valid id"
            )
        if market in (BUILTIN_MARKETPLACE, USER_MARKETPLACE):
            raise StoreError("marketplace_name_reserved", f"{market!r} is reserved")
        snapshot = self._snapshot(market, fetched.path)
        with self.lock:
            state = self.read_state()
            previous = state["marketplaces"].get(market) or {}
            entry: dict[str, Any] = {
                "id": market,
                "source": source,
                "url": fetched.url,
                "ref": ref,
                "commit": fetched.commit,
                "commit_reason": fetched.commit_reason,
                "pinned": bool(commit),
                "snapshot": str(snapshot),
                "format": probe.format,
                "description": probe.description,
                "default": default or bool(previous.get("default")),
                "parent": parent or previous.get("parent", ""),
                "trusted": default or bool(previous.get("trusted")),
                "trust_reason": "default_marketplace"
                if default
                else previous.get("trust_reason", ""),
                "trusted_digests": dict(previous.get("trusted_digests") or {}),
                "installed_at": previous.get("installed_at") or _now(),
                "updated_at": _now(),
                "diagnostics": [],
                "federated": [],
            }
            state["marketplaces"][market] = entry
            summary = self._apply(state, market, snapshot, entry)
            self._write_state(state)
        for pin in summary.pop("federated_pins", []):
            federated = self.install(pin.url, commit=pin.commit, name=pin.name, parent=market)
            summary.setdefault("federated", []).append(federated)
        return summary

    def _git_plugins(
        self, pins: list[GitPin], market: str, trusted: bool
    ) -> tuple[list[PluginRecord], list[Diagnostic]]:
        records: list[PluginRecord] = []
        diagnostics: list[Diagnostic] = []
        for pin in pins:
            try:
                fetched = fetch(
                    pin.url, cache=self.root / "sources", commit=pin.commit, declared=True
                )
            except SourceError as exc:
                diagnostics.append(
                    Diagnostic("error", "plugin_source_unavailable", str(exc), pin.name)
                )
                continue
            path = (fetched.path / pin.path).resolve() if pin.path else fetched.path
            if not within(fetched.path, path) or not path.is_dir():
                diagnostics.append(
                    Diagnostic(
                        "error",
                        "plugin_source_missing",
                        f"{pin.url}@{pin.commit}: no folder {pin.path!r}",
                        pin.name,
                    )
                )
                continue
            snapshot = self._snapshot(market, path)
            if (snapshot / "plugin.json").is_file():
                try:
                    records.append(load_plugin(snapshot, marketplace=market, trusted=trusted))
                except PluginLoadError as exc:
                    diagnostics.append(
                        Diagnostic("error", "plugin_load_failed", str(exc), pin.name)
                    )
            else:
                records.append(import_claude_plugin(snapshot, entry=pin.entry, marketplace=market))
        return records, diagnostics

    def _apply(
        self, state: dict[str, Any], market: str, snapshot: Path, entry: dict[str, Any]
    ) -> dict[str, Any]:
        trusted = bool(entry["trusted"])
        if entry["format"] == "claude-code":
            contents = read_claude_marketplace(snapshot, market)
        else:
            contents = self._read(snapshot, trusted=trusted, name=market)
        git_records, git_diags = self._git_plugins(contents.git_plugins, market, trusted)
        records = [*contents.plugins, *git_records]
        diagnostics = [*contents.diagnostics, *git_diags]
        for key in [k for k in state["plugins"] if k.split("/", 1)[0] == market]:
            if key.split("/", 1)[1] not in {record.id for record in records}:
                del state["plugins"][key]
        for key in [k for k in state["pending"] if k.split("/", 1)[0] == market]:
            del state["pending"][key]
        applied: list[str] = []
        held: list[str] = []
        for record in records:
            key = f"{market}/{record.id}"
            trusted_digest = entry["trusted_digests"].get(record.id)
            if entry.get("default"):
                entry["trusted_digests"][record.id] = record.executable_digest
            elif trusted and trusted_digest and trusted_digest != record.executable_digest:
                # Executable parts changed since trust: hold the update, keep the old record.
                state["pending"][key] = record.to_dict()
                held.append(record.id)
                continue
            state["plugins"][key] = record.to_dict()
            applied.append(record.id)
        for key in [k for k in state["agents"] if k.split("/", 1)[0] == market]:
            del state["agents"][key]
        for spec in contents.agents:
            state["agents"][f"{market}/{spec.id}"] = spec.to_dict()
        entry["diagnostics"] = [row.to_dict() for row in diagnostics]
        entry["federated"] = [
            {"name": pin.name, "url": pin.url, "commit": pin.commit} for pin in contents.federated
        ]
        return {
            "marketplace": market,
            "format": entry["format"],
            "trusted": trusted,
            "plugins": applied,
            "pending_trust": held,
            "agents": [spec.id for spec in contents.agents],
            "diagnostics": [row.to_dict() for row in diagnostics],
            "federated_pins": list(contents.federated),
        }

    def trust(self, market: str, *, reason: str = "explicit_trust_step") -> dict[str, Any]:
        """The explicit trust step: trust the marketplace's current executable parts.

        ``reason`` records who trusted it: the person in Settings or the CLI
        (``explicit_trust_step``), or a plugin Clio wrote, approved in its
        conversation or under bypass (:mod:`clio_agent.plugins.authored`).
        """

        with self.lock:
            state = self.read_state()
            entry = state["marketplaces"].get(market)
            if entry is None:
                raise StoreError(
                    "marketplace_not_installed", f"marketplace {market!r} is not installed"
                )
            snapshot = Path(entry["snapshot"])
            entry["trusted"] = True
            entry["trust_reason"] = reason
            for key, row in list(state["pending"].items()):
                if key.split("/", 1)[0] == market:
                    state["plugins"][key] = row
                    del state["pending"][key]
            entry["trusted_digests"] = {}
            summary = self._apply(state, market, snapshot, entry)
            for key, row in state["plugins"].items():
                if key.split("/", 1)[0] == market:
                    entry["trusted_digests"][row["id"]] = row["executable_digest"]
            entry["trusted_at"] = _now()
            self._write_state(state)
        summary.pop("federated_pins", None)
        return summary

    def update(self, market: str) -> dict[str, Any]:
        entry = self.marketplaces().get(market)
        if entry is None:
            raise StoreError(
                "marketplace_not_installed", f"marketplace {market!r} is not installed"
            )
        commit = entry["commit"] if entry.get("pinned") else ""
        return self.install(
            entry["source"],
            ref=entry.get("ref", ""),
            commit=commit,
            name=market,
            parent=entry.get("parent", ""),
        )

    def uninstall(self, market: str) -> None:
        with self.lock:
            state = self.read_state()
            if market not in state["marketplaces"]:
                raise StoreError(
                    "marketplace_not_installed", f"marketplace {market!r} is not installed"
                )
            del state["marketplaces"][market]
            for table in ("plugins", "pending", "agents"):
                for key in [k for k in state[table] if k.split("/", 1)[0] == market]:
                    del state[table][key]
            self._write_state(state)

    # ------------------------------------------------------------ user agents
    def save_user_agent(self, agent: UserAgent) -> Path:
        if not valid_plugin_id(agent.id):
            raise StoreError("user_agent_id_invalid", f"agent id {agent.id!r} is not a valid id")
        for value in (*agent.add, *agent.remove):
            PluginRef.parse(value)
        base = self.root / "user-agents"
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{agent.id}.json"
        path.write_text(json.dumps(agent.to_dict(), indent=1), encoding="utf-8")
        return path

    def delete_user_agent(self, agent_id: str) -> None:
        path = self.root / "user-agents" / f"{agent_id}.json"
        if not path.is_file():
            raise StoreError("user_agent_not_found", f"user agent {agent_id!r} does not exist")
        path.unlink()

    # ------------------------------------------------------------------ pins
    def save_resolution(self, digest: str, payload: dict[str, Any]) -> Path:
        base = self.root / "resolved"
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{digest}.json"
        if not path.is_file():
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            os.replace(temporary, path)
        return path

    def load_resolution(self, digest: str) -> dict[str, Any] | None:
        path = self.root / "resolved" / f"{digest}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
