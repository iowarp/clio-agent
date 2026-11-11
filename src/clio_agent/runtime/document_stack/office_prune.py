"""The parts of an extracted LibreOffice that headless conversion never reads.

Shared by ``office`` (every platform's layout) and Windows MSI extraction, which skips
these members. Like its siblings it imports only the standard library, so it also runs
standalone beside ``office.py``.
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path, PurePath

# Bundled extensions a headless converter never loads: the Calc NLP solver and the
# MediaWiki publisher (about 3,000 files together).
UNREAD_EXTENSIONS = frozenset({"nlpsolver", "wiki-publisher"})
# Spelling (.aff/.dic) and thesaurus (.dat/.idx) data inside the dict-* extensions:
# about 440 MB that no conversion reads. Hyphenation patterns (hyph_*.dic) change line
# breaks and pagination, so they stay.
UNREAD_DICTIONARY_SUFFIXES = frozenset({".aff", ".dic", ".dat", ".idx"})


def _remove_tree(path: Path) -> int:
    files = sum(len(names) for _, _, names in os.walk(path))
    shutil.rmtree(path)
    return files


def unread(relative: PurePath) -> bool:
    """Whether ``prune_unread`` would remove this file of an image (a path below it).

    Windows extraction skips these members, so they are never written and scanned
    only to be deleted again.
    """
    parts = relative.parts
    lowered = [part.lower() for part in parts]
    if lowered[0] == "help":
        return True
    for index, part in enumerate(parts[:-1]):
        parent = parts[index - 1] if index else ""
        if parent == "extensions" and part in UNREAD_EXTENSIONS:
            return True
        if (
            parent == "resource"
            and index + 1 < len(parts) - 1
            and parts[index + 1] == "LC_MESSAGES"
            and not lowered[index].startswith("en")
        ):
            return True
    name = lowered[-1]
    folder = parts[-2] if len(parts) > 1 else ""
    grandparent = parts[-3] if len(parts) > 2 else ""
    if folder.startswith("dict-") and grandparent == "extensions":
        return PurePath(name).suffix in UNREAD_DICTIONARY_SUFFIXES and not name.startswith("hyph")
    if folder == "res" and grandparent == "registry" and name.endswith(".xcd"):
        language = PurePath(name).stem.rpartition("_")[2]
        return bool(language) and not language.startswith("en")
    return False


def prune_unread(image: Path) -> int:
    """Remove the parts of an extracted LibreOffice that headless conversion never reads.

    Every installed file costs a write and, on Windows, an antivirus scan (gact-tui#739).
    The same list applies to all platforms' layouts: the offline help, the
    ``UNREAD_EXTENSIONS``, the spelling and thesaurus data of the ``dict-*``
    extensions, the non-English UI translations (``resource/<lang>``) and their
    configuration packs (``registry/res/*_<lang>.xcd``, which every start reads: about
    2 s per cold conversion). The application, filters, fonts, hyphenation patterns and
    English (``en*``) stay, so conversions are pixel-identical. Returns the number of
    files removed.

    macOS is skipped: LibreOffice.app is TDF's notarized bundle, and removing any
    sealed resource fails ``codesign --verify`` and Gatekeeper.
    """
    if platform.system() == "Darwin":
        return 0
    removed = 0
    for directory, names, files in os.walk(image):
        path = Path(directory)
        if path.name == "extensions":
            for name in sorted(UNREAD_EXTENSIONS.intersection(names)):
                removed += _remove_tree(path / name)
        elif path.name.startswith("dict-") and path.parent.name == "extensions":
            for name in files:
                lowered = name.lower()
                if Path(lowered).suffix in UNREAD_DICTIONARY_SUFFIXES and not lowered.startswith(
                    "hyph"
                ):
                    (path / name).unlink()
                    removed += 1
        elif path.name == "res" and path.parent.name == "registry":
            for name in files:
                language = Path(name).stem.rpartition("_")[2].lower()
                if name.endswith(".xcd") and language and not language.startswith("en"):
                    (path / name).unlink()
                    removed += 1
        elif path.name == "resource":
            for name in sorted(names):
                if not name.lower().startswith("en") and (path / name / "LC_MESSAGES").is_dir():
                    removed += _remove_tree(path / name)
        elif "program" in names and "help" in names:
            removed += _remove_tree(path / "help")
        # Translations and extensions are near the top of every layout; skip the rest.
        names[:] = [
            name
            for name in names
            if (path / name).exists() and len((path / name).relative_to(image).parts) < 6
        ]
    return removed
