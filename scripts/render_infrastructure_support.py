"""Render the managed-infrastructure support matrix from CLIO's own service catalog.

The tables on the website's managed-infrastructure page, between the generated markers, come from
``service_definitions`` evaluated for each host profile below, so the document cannot
claim support the catalog does not offer. ``tests/test_scripts`` fails when they drift.

Usage: ``uv run python scripts/render_infrastructure_support.py [--check]``
"""

from __future__ import annotations

import sys
from pathlib import Path

from clio_agent.gact.infrastructure.drivers import service_definitions
from clio_agent.gact.infrastructure.models import ContainerRuntimeFact, TargetFacts

DOC = Path(__file__).resolve().parents[1] / "site/src/content/docs/docs/managed-infrastructure.mdx"
BEGIN = "{/* BEGIN GENERATED: support tables (scripts/render_infrastructure_support.py) */}"
END = "{/* END GENERATED: support tables */}"

#: (heading, os, arch, accelerator). Linux hosts are SSH/HPC targets; Windows and macOS
#: are the machine running CLIO, which is where their native services install.
PROFILES = (
    ("Linux x86-64 with an NVIDIA GPU", "linux", "x86_64", "nvidia"),
    ("Linux x86-64 with an AMD GPU", "linux", "x86_64", "amd"),
    ("Linux x86-64 with an Intel GPU", "linux", "x86_64", "intel"),
    ("Linux x86-64 without a GPU", "linux", "x86_64", "none"),
    ("Linux ARM64 with an NVIDIA GPU", "linux", "aarch64", "nvidia"),
    ("Linux ARM64 without a GPU", "linux", "aarch64", "none"),
    ("Windows x64 with an NVIDIA GPU", "windows", "x86_64", "nvidia"),
    ("Windows x64 with an AMD GPU", "windows", "x86_64", "amd"),
    ("Windows x64 without a GPU", "windows", "x86_64", "none"),
    ("Windows on ARM", "windows", "aarch64", "none"),
    ("macOS on Apple silicon", "macos", "aarch64", "none"),
    ("macOS on Intel", "macos", "x86_64", "none"),
)


def _facts(os: str, arch: str, accelerator: str, *, containers: bool) -> TargetFacts:
    windows = os == "windows"
    return TargetFacts(
        target_id="remote" if os == "linux" else "local",
        label=os,
        os=os,
        arch=arch,
        accelerator=accelerator,
        uv_available=True,
        docker_available=containers,
        docker_installed=containers,
        container_runtimes=[ContainerRuntimeFact(name="docker", installed=True, usable=True)]
        if containers
        else [],
        home="C:\\Users\\person" if windows else "/home/person",
        agent_data_root="C:\\CLIO" if windows else "/data/clio",
        hostname="host",
    )


def _offered(facts: TargetFacts) -> dict[str, tuple[str, list[str]]]:
    return {
        row.id: (row.label, [f"{v.label} ({v.version})" for v in row.variants if v.compatible])
        for row in service_definitions(facts)
        # Remote access installs CLIO itself on another server; the page says so in prose,
        # since a per-computer row would read as "CLIO is not available here".
        if row.category != "remote_access"
    }


def render() -> str:
    """Return the generated section, one table per host profile."""
    lines = [BEGIN, ""]
    for heading, os, arch, accelerator in PROFILES:
        native = _offered(_facts(os, arch, accelerator, containers=False))
        containers = _offered(_facts(os, arch, accelerator, containers=True))
        lines += [
            f"### {heading}",
            "",
            "| Service | Installs directly | Also available with Docker, Podman or Apptainer |",
            "| --- | --- | --- |",
        ]
        for service, (label, without) in native.items():
            added = [item for item in containers[service][1] if item not in without]
            lines.append(
                f"| {label} | {'; '.join(without) or 'Not available'} | "
                f"{'; '.join(added) or 'Nothing more'} |"
            )
        lines.append("")
    lines.append(END)
    return "\n".join(lines)


def splice(document: str) -> str:
    """Replace the generated section of ``document`` with the current rendering."""
    start, end = document.index(BEGIN), document.index(END) + len(END)
    return document[:start] + render() + document[end:]


def main() -> int:
    """Rewrite the document, or with ``--check`` fail when it is out of date."""
    current = DOC.read_text(encoding="utf-8")
    updated = splice(current)
    if "--check" in sys.argv[1:]:
        if updated != current:
            print(f"{DOC} is out of date: run scripts/render_infrastructure_support.py")
            return 1
        return 0
    DOC.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
