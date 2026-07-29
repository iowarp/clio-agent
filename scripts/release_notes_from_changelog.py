"""Print the CHANGELOG.md section for a release tag (the release-notes backstop).

Used by the ``release-check`` job in ``clio-bundles.yml``: when a freshly-created
GitHub release still has a bare body (the tag/commit subject ``action-gh-release``
defaults to), the job fills it with this script's output — the ``## [X.Y.Z]``
section of ``CHANGELOG.md`` plus a CHANGELOG link — so a release page is never
just a merge-commit subject. The curated notes authored by the ``release-clio``
skill (step 6b) remain the standard and are never overwritten.

Usage: ``python scripts/release_notes_from_changelog.py v0.9.0`` (leading ``v``
optional). Exits 1 (printing nothing to stdout) when the section is absent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def section_for(version: str) -> str | None:
    """Return the ``## [version]`` CHANGELOG section body, or ``None`` if absent."""

    text = CHANGELOG.read_text(encoding="utf-8")
    heading = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n", text, flags=re.MULTILINE)
    if heading is None:
        return None
    rest = text[heading.end() :]
    nxt = re.search(r"^## \[", rest, flags=re.MULTILINE)
    body = rest[: nxt.start()] if nxt else rest
    return body.strip()


def main() -> int:
    """CLI entry: print the section for ``sys.argv[1]`` or exit 1."""

    if len(sys.argv) != 2:
        print("usage: release_notes_from_changelog.py <tag>", file=sys.stderr)
        return 2
    # CHANGELOG carries arrows/dashes; Windows consoles default to cp1252.
    sys.stdout.reconfigure(encoding="utf-8")
    version = sys.argv[1].lstrip("v")
    body = section_for(version)
    if body is None:
        print(f"no CHANGELOG section for {version!r}", file=sys.stderr)
        return 1
    link = (
        "\n\n**Full details:** "
        f"[CHANGELOG {version}](https://github.com/iowarp/clio-agent/blob/main/CHANGELOG.md)"
    )
    print(body + link)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
