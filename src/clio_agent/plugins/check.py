"""``clio-agent plugin check``: the install resolver, run in the editor and in CI.

Checks a plugin folder, a marketplace folder, or an installed marketplace. For
every plugin it loads the package exactly as install does and resolves it
beside ``core``; for every agent class it runs ``spec()`` and the full stage 2
resolution. Unresolved ids, missing prompt files, tool collisions, hook cycles,
untrusted executable parts and fragment size budgets fail (or warn) here
instead of in a user's session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clio_agent.plugins.loader import PluginLoadError
from clio_agent.plugins.marketplace import read_marketplace
from clio_agent.plugins.records import Diagnostic, PluginRecord
from clio_agent.plugins.resolve import ResolutionError, resolve_agent
from clio_agent.plugins.service import PluginService
from clio_agent.sdk.agent import AgentSpec, PluginSelection
from clio_agent.sdk.versions import PluginRef


@dataclass
class CheckTarget:
    """One checked unit (a plugin or an agent) and what was found."""

    kind: str
    id: str
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(row.severity == "error" for row in self.diagnostics)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "ok": self.ok,
            "diagnostics": [row.to_dict() for row in self.diagnostics],
        }


@dataclass
class CheckReport:
    source: str
    targets: list[CheckTarget] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(t.ok for t in self.targets) and not any(
            r.severity == "error" for r in self.diagnostics
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ok": self.ok,
            "targets": [target.to_dict() for target in self.targets],
            "diagnostics": [row.to_dict() for row in self.diagnostics],
        }

    def render(self) -> str:
        lines = [f"plugin check {self.source}"]
        for row in self.diagnostics:
            lines.append(f"  {row.severity.upper()}: {row}")
        for target in self.targets:
            mark = "ok  " if target.ok else "FAIL"
            lines.append(f"{mark} {target.kind} {target.id}")
            for row in target.diagnostics:
                lines.append(f"     {row.severity}: {row.message} [{row.code}]")
        passed = sum(1 for t in self.targets if t.ok)
        lines.append(
            f"{passed}/{len(self.targets)} passed" + ("" if self.ok else " -- check failed")
        )
        return "\n".join(lines)


def _probe_spec(plugin: PluginRecord) -> AgentSpec:
    plugins = [PluginSelection(PluginRef.parse("core"), "core")]
    if plugin.id != "core":
        plugins.append(PluginSelection(PluginRef(plugin.id, ""), "check"))
    return AgentSpec(
        id=f"check-{plugin.id}",
        version="0.0.0",
        name=f"check {plugin.id}",
        role="a plugin check probe",
        description="",
        flow={"id": "react", "options": {}},
        plugins=tuple(plugins),
        layers=("core", "check"),
    )


def _resolve(
    service: PluginService, spec: AgentSpec, market: str, records: list[PluginRecord]
) -> list[Diagnostic]:
    try:
        resolution = resolve_agent(service.catalog(records), spec, marketplace=market)
    except ResolutionError as exc:
        return list(exc.diagnostics)
    return [Diagnostic(**row) for row in resolution.diagnostics]


def check(
    path: Path | None = None, *, installed: str = "", service: PluginService | None = None
) -> CheckReport:
    """Check a folder (``path``) or an installed marketplace (``installed``)."""

    service = service or PluginService()
    if installed:
        entry = service.store.marketplaces().get(installed)
        if entry is None:
            report = CheckReport(source=installed)
            report.diagnostics.append(
                Diagnostic(
                    "error",
                    "marketplace_not_installed",
                    f"{installed!r} is not installed",
                    installed,
                )
            )
            return report
        report = CheckReport(source=f"installed marketplace {installed}")
        records = [r for r in service.store.records() if r.marketplace == installed]
        report.diagnostics.extend(Diagnostic(**row) for row in entry.get("diagnostics") or [])
        for row in service.store.pending():
            if row.marketplace == installed:
                report.diagnostics.append(
                    Diagnostic(
                        "warning",
                        "update_pending_trust",
                        "an update changes executable parts and waits for the trust step",
                        row.id,
                    )
                )
        specs = [
            spec
            for key, spec in service.store.agent_specs().items()
            if key.split("/", 1)[0] == installed
        ]
        market = installed
        extra: list[PluginRecord] = []
    else:
        assert path is not None
        report = CheckReport(source=str(path))
        try:
            contents = read_marketplace(path.resolve(), trusted=True)
        except (PluginLoadError, OSError, json.JSONDecodeError) as exc:
            report.diagnostics.append(Diagnostic("error", "plugin_invalid", str(exc), path.name))
            return report
        report.diagnostics.extend(contents.diagnostics)
        if contents.git_plugins or contents.federated:
            report.diagnostics.append(
                Diagnostic(
                    "warning",
                    "git_sources_not_checked",
                    f"{len(contents.git_plugins)} git plugin(s) and {len(contents.federated)} federated "
                    "marketplace(s) are checked after install (plugin check --installed)",
                    contents.name,
                )
            )
        records = contents.plugins
        specs = contents.agents
        market = contents.name
        extra = records
    for record in records:
        target = CheckTarget("plugin", f"{record.id}@{record.version}")
        target.diagnostics.extend(record.diagnostics)
        if record.valid:
            seen = {(d.code, d.message) for d in target.diagnostics}
            for found in _resolve(service, _probe_spec(record), market, extra):
                if (found.code, found.message) not in seen and (
                    found.plugin in ("", record.id) or found.severity == "error"
                ):
                    target.diagnostics.append(found)
        report.targets.append(target)
    for spec in specs:
        target = CheckTarget("agent", f"{market}/{spec.id}")
        target.diagnostics.extend(_resolve(service, spec, market, extra))
        report.targets.append(target)
    return report
