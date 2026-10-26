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

from clio_agent.platform_paths import rename_extended, rmtree_extended, win_extended_path
from clio_agent.plugins.claude_code import import_claude_plugin
from clio_agent.plugins.loader import PluginLoadError, load_plugin, tree_digests
from clio_agent.plugins.manifest import within
from clio_agent.plugins.marketplace import (
    GitPin,
    MarketplaceContents,
    load_agent_specs,
    read_marketplace,
)
from clio_agent.plugins.package_files import ignored_package_names
from clio_agent.plugins.records import Diagnostic, PluginRecord
from clio_agent.plugins.sources import SourceError, fetch
from clio_agent.sdk.agent import AgentSpec
from clio_agent.sdk.versions import PluginRef, valid_plugin_id

BUILTIN_ROOT = Path(__file__).resolve().parent / "builtin"
BUILTIN_MARKETPLACE = "clio-builtin"
USER_MARKETPLACE = "user"
#: ``trusted_digests`` key for a marketplace's ``agents/`` folder (not a valid plugin id).
AGENTS_DIGEST = "@agents"
STATE_VERSION = 1
#: The reader's version. Bump it when reading an installed snapshot gives a different
#: record than before (a hook event the importer now maps, say): installed
#: marketplaces are then re-read from their own snapshots, without fetching.
READER_VERSION = 2


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
    try:
        shutil.copytree(
            win_extended_path(source.absolute()),
            win_extended_path(staging.absolute()),
            symlinks=False,
            ignore=ignored_package_names,
        )
        rename_extended(staging, target)
    except OSError:
        rmtree_extended(staging, ignore_errors=True)
        if not target.is_dir():
            raise
        # Another install of the same content finished first: its copy is identical.


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
            if (
                previous
                and not default
                and (previous.get("url") != fetched.url or previous.get("parent", "") != parent)
            ):
                # A name is the marketplace's identity and carries its trust: another
                # source (or another federating parent) never takes it over.
                raise StoreError(
                    "marketplace_name_taken",
                    f"a marketplace named {market!r} is already installed from "
                    f"{previous.get('url')}; remove it before installing {fetched.url}",
                )
            entry: dict[str, Any] = {
                "id": market,
                "source": fetched.url if fetched.local else source,
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
            try:
                federated = self.install(pin.url, commit=pin.commit, name=pin.name, parent=market)
            except StoreError as exc:
                # One unreachable or refused child never undoes its parent's install.
                summary["diagnostics"].append(
                    Diagnostic("error", f"federated_{exc.code}", str(exc), pin.name).to_dict()
                )
                continue
            summary.setdefault("federated", []).append(federated)
        return summary

    def _git_plugins(
        self, pins: list[GitPin], market: str
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
                    records.append(load_plugin(snapshot, marketplace=market, trusted=False))
                except PluginLoadError as exc:
                    diagnostics.append(
                        Diagnostic("error", "plugin_load_failed", str(exc), pin.name)
                    )
            else:
                records.append(import_claude_plugin(snapshot, entry=pin.entry, marketplace=market))
        return records, diagnostics

    def _apply(
        self,
        state: dict[str, Any],
        market: str,
        snapshot: Path,
        entry: dict[str, Any],
        *,
        approve: bool = False,
    ) -> dict[str, Any]:
        """Apply ``snapshot`` as ``market``'s content.

        Everything is read first without importing any Python. Only then do the
        executable digests decide what may run: a trusted marketplace's plugin
        (and its ``agents/`` folder) is imported when its digest is the one that was
        trusted, or when ``approve`` (the trust step, or the shipped default) trusts
        the content as it is now. Changed or new executable content is held for the
        trust step without having run.
        """

        trusted = bool(entry["trusted"])
        approve = trusted and (approve or bool(entry.get("default")))
        digests: dict[str, str] = entry["trusted_digests"]
        if approve:
            digests.clear()
        # The detected layout decides how to read it: a Claude Code marketplace, a
        # single Claude Code plugin, a folder of skills, or an Agent Plugins tree.
        contents = self._read(snapshot, trusted=False, name=market)
        git_records, git_diags = self._git_plugins(contents.git_plugins, market)
        diagnostics = [*contents.diagnostics, *git_diags]
        records: list[PluginRecord] = []
        held: list[str] = []
        for record in [*contents.plugins, *git_records]:
            if approve:
                digests[record.id] = record.executable_digest
            known = digests.get(record.id)
            if trusted and known == record.executable_digest:
                record = self._load_trusted(record, market, diagnostics)
            elif trusted:
                held.append(record.id)
                if known:
                    # Executable parts changed since trust: hold the update, keep the old record.
                    state["pending"][f"{market}/{record.id}"] = record.to_dict()
                    continue
            records.append(record)
        for key in [k for k in state["plugins"] if k.split("/", 1)[0] == market]:
            if key.split("/", 1)[1] not in {record.id for record in records} | set(held):
                del state["plugins"][key]
        held_now = {f"{market}/{rid}" for rid in held}
        for key in [k for k in state["pending"] if k.split("/", 1)[0] == market]:
            if key not in held_now:
                del state["pending"][key]
        for record in records:
            state["plugins"][f"{market}/{record.id}"] = record.to_dict()
        agents = self._agents(state, market, snapshot, entry, approve, diagnostics)
        entry["diagnostics"] = [row.to_dict() for row in diagnostics]
        entry["federated"] = [
            {"name": pin.name, "url": pin.url, "commit": pin.commit} for pin in contents.federated
        ]
        return {
            "marketplace": market,
            "format": entry["format"],
            "trusted": trusted,
            "plugins": [record.id for record in records if record.id not in held],
            "pending_trust": held + (["agents"] if agents is None else []),
            "agents": agents or [],
            "diagnostics": [row.to_dict() for row in diagnostics],
            "federated_pins": list(contents.federated),
        }

    @staticmethod
    def _load_trusted(
        record: PluginRecord, market: str, diagnostics: list[Diagnostic]
    ) -> PluginRecord:
        if record.format != "agent-plugins" or not record.python_pending:
            return record
        try:
            return load_plugin(Path(record.root), marketplace=market, trusted=True)
        except PluginLoadError as exc:
            diagnostics.append(Diagnostic("error", "plugin_invalid", str(exc), record.id))
            return record

    @staticmethod
    def _agents(
        state: dict[str, Any],
        market: str,
        snapshot: Path,
        entry: dict[str, Any],
        approve: bool,
        diagnostics: list[Diagnostic],
    ) -> list[str] | None:
        """Import the marketplace's ``agents/`` when trusted as it is. ``None`` = held."""

        folder = snapshot / "agents"
        digest = tree_digests(folder)[0] if folder.is_dir() else ""
        digests: dict[str, str] = entry["trusted_digests"]
        if approve and digest:
            digests[AGENTS_DIGEST] = digest
        keys = [k for k in state["agents"] if k.split("/", 1)[0] == market]
        if digest and entry["trusted"] and digests.get(AGENTS_DIGEST) != digest:
            return None  # changed since trust: the trusted agents stay until the trust step
        for key in keys:
            del state["agents"][key]
        if not digest or not entry["trusted"] or entry["format"] == "claude-code":
            return []
        diagnostics[:] = [d for d in diagnostics if d.code != "marketplace_untrusted"]
        specs, agent_diags = load_agent_specs(snapshot, market)
        diagnostics.extend(agent_diags)
        for spec in specs:
            state["agents"][f"{market}/{spec.id}"] = spec.to_dict()
        return [spec.id for spec in specs]

    def trust(
        self, market: str, *, reason: str = "explicit_trust_step", snapshot: str = ""
    ) -> dict[str, Any]:
        """The explicit trust step: trust the marketplace's current executable parts.

        ``reason`` records who trusted it: the person in Settings or the CLI
        (``explicit_trust_step``), or a plugin Clio wrote, approved in its
        conversation or under bypass (:mod:`clio_agent.plugins.authored`).
        ``snapshot``, when given, is the content that was reviewed: a marketplace
        whose installed content changed since is refused, never trusted unseen.
        """

        with self.lock:
            state = self.read_state()
            entry = state["marketplaces"].get(market)
            if entry is None:
                raise StoreError(
                    "marketplace_not_installed", f"marketplace {market!r} is not installed"
                )
            if snapshot and entry["snapshot"] != snapshot:
                raise StoreError(
                    "trust_content_changed",
                    f"marketplace {market!r} changed after it was reviewed; review it again",
                )
            current = Path(entry["snapshot"])
            entry["trusted"] = True
            entry["trust_reason"] = reason
            summary = self._apply(state, market, current, entry, approve=True)
            entry["trusted_at"] = _now()
            self._write_state(state)
        summary.pop("federated_pins", None)
        return summary

    def reread_installed(self) -> list[dict[str, Any]]:
        """Re-read every installed marketplace from its snapshot when the reader changed.

        Trust carries over through the executable digests: content that has not
        changed keeps its trust, and a record whose executable parts now read
        differently is held for the trust step as on any update. Returns one result
        per re-read marketplace; an unreadable snapshot is reported, not skipped.
        """

        with self.lock:
            state = self.read_state()
            if state.get("reader") == READER_VERSION or not state["marketplaces"]:
                return []
            results: list[dict[str, Any]] = []
            for market, entry in state["marketplaces"].items():
                snapshot = Path(str(entry.get("snapshot") or ""))
                if not snapshot.is_dir():
                    entry["diagnostics"] = [
                        *entry.get("diagnostics", []),
                        Diagnostic(
                            "error", "snapshot_missing", f"{snapshot} is not on disk", market
                        ).to_dict(),
                    ]
                    continue
                try:
                    results.append(self._apply(state, market, snapshot, entry))
                except StoreError as exc:
                    results.append({"marketplace": market, "error": exc.code, "message": str(exc)})
            state["reader"] = READER_VERSION
            self._write_state(state)
            return results

    def update(self, market: str) -> dict[str, Any]:
        entry = self.marketplaces().get(market)
        if entry is None:
            raise StoreError(
                "marketplace_not_installed", f"marketplace {market!r} is not installed"
            )
        commit = entry["commit"] if entry.get("pinned") else ""
        return self.install(
            entry.get("url") or entry["source"],
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
    def save_user_agent(self, agent: UserAgent, *, create_only: bool = False) -> Path:
        """Publish one local definition atomically; create-only refuses name collisions."""
        if not valid_plugin_id(agent.id):
            raise StoreError("user_agent_id_invalid", f"agent id {agent.id!r} is not a valid id")
        for value in (*agent.add, *agent.remove):
            PluginRef.parse(value)
        base = self.root / "user-agents"
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{agent.id}.json"
        with self.lock:
            if create_only and path.exists():
                raise StoreError(
                    "user_agent_exists", f"Agent {agent.id!r} already exists; choose another name"
                )
            temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps(agent.to_dict(), indent=1), encoding="utf-8")
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return path

    def delete_user_agent(self, agent_id: str) -> None:
        path = self.root / "user-agents" / f"{agent_id}.json"
        if not path.is_file():
            raise StoreError("user_agent_not_found", f"user agent {agent_id!r} does not exist")
        path.unlink()

    # ------------------------------------------------------------------ pins
    def save_resolution(self, digest: str, payload: dict[str, Any]) -> Path:
        """Pin a resolution atomically, with staging owned by this writer."""
        base = self.root / "resolved"
        base.mkdir(parents=True, exist_ok=True)
        path = base / f"{digest}.json"
        if not path.is_file():
            temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
                with self.lock:
                    if not path.is_file():
                        os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return path

    def load_resolution(self, digest: str) -> dict[str, Any] | None:
        path = self.root / "resolved" / f"{digest}.json"
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
