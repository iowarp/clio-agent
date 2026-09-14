"""Package the completed v0.9.2 live evidence without changing the candidate."""

from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / "out" / "live-verification" / "release_v0_9_2"
CAMPAIGN = ROOT / "scripts" / "live_verification" / "release_v0_9_2"
ARCHIVE = OUT.parent / "clio-agent-v0.9.2-live-verification.zip"

EVIDENCE_FILES = (
    "manifest.json",
    "verdict.md",
    "release-notes.md",
    "focused-gates.xml",
    "conversion_gate.json",
    "plan_gate.json",
    "mcp_apps_final.json",
    "tool-triage-final-messages.json",
    "tool-workflow-final-messages.json",
)
EVIDENCE_DIRECTORIES = (
    "screenshots",
    "compaction",
    "earthscope-final-rerun",
    "factorio-final-candidate",
    "dist-2aed4061",
    "schema-dist-f9945006",
)


def iter_archive_files(evidence_root: Path, campaign_root: Path) -> list[tuple[Path, Path]]:
    """Return source and archive paths for the curated release evidence."""

    members: list[tuple[Path, Path]] = []
    for name in EVIDENCE_FILES:
        source = evidence_root / name
        if source.is_file():
            members.append((source, Path("evidence") / name))
    for name in EVIDENCE_DIRECTORIES:
        directory = evidence_root / name
        if not directory.is_dir():
            continue
        for source in sorted(path for path in directory.rglob("*") if path.is_file()):
            members.append((source, Path("evidence") / name / source.relative_to(directory)))
    for source in sorted(path for path in campaign_root.rglob("*") if path.is_file()):
        members.append((source, Path("campaign") / source.relative_to(campaign_root)))
    return members


def main() -> int:
    """Create a zip beside the evidence directory after validating required files."""

    required = [OUT / "manifest.json", OUT / "verdict.md"]
    missing = [path.name for path in required if not path.is_file()]
    if missing:
        raise SystemExit(f"cannot package incomplete evidence: {', '.join(missing)}")
    members = iter_archive_files(OUT, CAMPAIGN)
    with ZipFile(ARCHIVE, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for source, archive_path in members:
            archive.write(source, archive_path.as_posix())
    print(ARCHIVE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
