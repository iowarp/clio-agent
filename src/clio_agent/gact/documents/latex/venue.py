"""Venue checks from a paper template's rules (LaTeX plan, "Venue rules").

Templates are plugin data: a ``templates/<name>/template.yaml`` folder in a plugin the session
has. Nothing here names a venue. A template's ``rules`` declare what a build can check:

- ``page_limit`` with ``references_count``: pages counted up to the bibliography, or all;
- ``double_blind``: no author name outside the title block, no acknowledgments section, and
  URLs reported as things to check;
- ``required_packages`` and ``forbidden_packages``;
- ``paper_size``: ``letter`` or ``a4``.

Each check is ``passed``, ``failed`` (with where it failed) or ``to_check`` when it needs a
person's judgement, such as whether a URL identifies the authors.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

__all__ = ["check_venue", "find_template", "session_template_roots"]

_PAPER_SIZES = {"letter": (612.0, 792.0), "a4": (595.28, 841.89)}
_SIZE_TOLERANCE = 3.0
_REFERENCE_HEADINGS = re.compile(r"^\s*(references|bibliography)\s*$", re.IGNORECASE | re.M)
_USEPACKAGE = re.compile(r"\\usepackage(?:\[[^\]]*\])?\{([^}]*)\}")
_AUTHOR = re.compile(r"\\author(?:\[[^\]]*\])?\{")
_ACKS = re.compile(
    r"\\(?:section|subsection)\*?\{\s*acknowledge?ments?\s*\}|\\begin\{acks\}", re.IGNORECASE
)
_URL = re.compile(r"\\(?:url|href)\{([^}]*)\}|https?://[^\s}]+")
_NAME_COMMANDS = re.compile(
    r"\\(?:IEEEauthorblock[NA]|affiliation|institution|country|email|orcid|thanks|inst"
    r"|address|and|IEEEauthorrefmark|authornote)\b(?:\{[^{}]*\})?"
)


def session_template_roots() -> list[Path]:
    """The ``templates`` folders of the active session's plugins."""
    from clio_agent.gact import context as _ctx  # noqa: PLC0415
    from clio_agent.gact.agents.resolution import (  # noqa: PLC0415
        SessionAgentUnavailable,
        session_resolution,
    )

    app, session_id = _ctx.active_app(), _ctx.active_session_id()
    if app is None or not session_id:
        return []
    try:
        resolution = session_resolution(app, session_id)
    except SessionAgentUnavailable:
        return []
    roots = []
    for row in resolution.plugins:
        folder = Path(str(row.get("root") or "")) / "templates"
        if folder.is_dir():
            roots.append(folder)
    return roots


def find_template(name: str, roots: Sequence[Path]) -> tuple[Path, dict[str, Any]] | None:
    """The template folder and its parsed ``template.yaml`` for ``name``, if a plugin has it."""
    import yaml  # noqa: PLC0415 - only for a venue build

    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name or ""):
        return None
    for root in roots:
        folder = root / name
        manifest = folder / "template.yaml"
        if manifest.is_file():
            data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                return folder, data
    return None


def _strip_comments(text: str) -> str:
    return "\n".join(re.sub(r"(?<!\\)%.*$", "", line) for line in text.splitlines())


def _braced(text: str, start: int) -> tuple[str, int]:
    """The balanced ``{...}`` group starting at ``text[start] == '{'``."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index], index + 1
    return text[start + 1 :], len(text)


def _author_names(texts: dict[str, str]) -> list[str]:
    names: list[str] = []
    for text in texts.values():
        for match in _AUTHOR.finditer(text):
            body, _ = _braced(text, match.end() - 1)
            body = _NAME_COMMANDS.sub(" ", body)
            body = re.sub(r"\\[a-zA-Z]+\*?", " ", body)
            for part in re.split(r"[,\n{}\\]|\band\b", body):
                name = " ".join(part.split())
                if len(name.split()) >= 2 and len(name) <= 60:
                    names.append(name)
    return list(dict.fromkeys(names))


def _without_author_blocks(text: str) -> str:
    """``text`` with each ``\\author{...}`` body blanked, keeping its line breaks."""
    out = list(text)
    for match in _AUTHOR.finditer(text):
        _, end = _braced(text, match.end() - 1)
        for index in range(match.start(), end):
            if out[index] != "\n":
                out[index] = " "
    return "".join(out)


def _locations(texts: dict[str, str], pattern: re.Pattern[str]) -> list[tuple[str, int, str]]:
    found = []
    for name, text in texts.items():
        for number, line in enumerate(text.splitlines(), start=1):
            for match in pattern.finditer(line):
                found.append((name, number, match.group(0)))
    return found


def _check(rule: str, status: str, message: str, file: str = "", line: int = 0) -> dict[str, Any]:
    row: dict[str, Any] = {"rule": rule, "status": status, "message": message}
    if file:
        row["file"], row["line"] = file, line
    return row


def _page_checks(rules: dict[str, Any], pdf: Path, page_count: int) -> list[dict[str, Any]]:
    from pypdf import PdfReader  # noqa: PLC0415 - heavy import on demand

    checks: list[dict[str, Any]] = []
    reader = PdfReader(str(pdf))
    limit = rules.get("page_limit")
    if isinstance(limit, int) and limit > 0:
        counted = page_count
        if not rules.get("references_count", True):
            for index, page in enumerate(reader.pages):
                if _REFERENCE_HEADINGS.search(page.extract_text() or ""):
                    counted = index + 1
                    break
        what = (
            "pages, references included"
            if rules.get("references_count", True)
            else "pages before the references"
        )
        checks.append(
            _check(
                "Page limit",
                "passed" if counted <= limit else "failed",
                f"{counted} {what}; the limit is {limit}.",
            )
        )
    size = str(rules.get("paper_size") or "").lower()
    if size in _PAPER_SIZES and reader.pages:
        box = reader.pages[0].mediabox
        width, height = float(box.width), float(box.height)
        want_w, want_h = _PAPER_SIZES[size]
        ok = abs(width - want_w) <= _SIZE_TOLERANCE and abs(height - want_h) <= _SIZE_TOLERANCE
        checks.append(
            _check(
                "Paper size",
                "passed" if ok else "failed",
                f"The PDF is {width:.0f} x {height:.0f} pt; {size} is "
                f"{want_w:.0f} x {want_h:.0f} pt.",
            )
        )
    return checks


def _source_checks(rules: dict[str, Any], texts: dict[str, str]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    used = {
        package.strip()
        for text in texts.values()
        for match in _USEPACKAGE.finditer(text)
        for package in match.group(1).split(",")
    }
    for package in rules.get("required_packages") or []:
        checks.append(
            _check(
                f"Requires {package}",
                "passed" if package in used else "failed",
                f"{package} is {'loaded' if package in used else 'not loaded'}.",
            )
        )
    for package in rules.get("forbidden_packages") or []:
        hits = [
            (name, number)
            for name, number, _ in _locations(
                texts, re.compile(r"\\usepackage(?:\[[^\]]*\])?\{[^}]*\b" + re.escape(package))
            )
        ]
        checks.append(
            _check(
                f"Forbids {package}",
                "failed" if hits else "passed",
                f"{package} is loaded." if hits else f"{package} is not loaded.",
                *(hits[0] if hits else ("", 0)),
            )
        )
    if rules.get("double_blind"):
        names = _author_names(texts)
        outside = {name: _without_author_blocks(text) for name, text in texts.items()}
        planted: list[tuple[str, int, str]] = []
        for author in names:
            planted.extend(_locations(outside, re.compile(re.escape(author), re.IGNORECASE)))
        if planted:
            file, number, text = planted[0]
            checks.append(
                _check(
                    "Double-blind: author names",
                    "failed",
                    f"The author name {text!r} appears outside the title block"
                    + (f" ({len(planted)} places)." if len(planted) > 1 else "."),
                    file,
                    number,
                )
            )
        else:
            checks.append(
                _check(
                    "Double-blind: author names",
                    "passed",
                    "No author name appears outside the title block.",
                )
            )
        acks = _locations(texts, _ACKS)
        checks.append(
            _check(
                "Double-blind: acknowledgments",
                "failed" if acks else "passed",
                "An acknowledgments section is present." if acks else "No acknowledgments.",
                *(acks[0][:2] if acks else ("", 0)),
            )
        )
        urls = _locations(texts, _URL)
        if urls:
            file, number, text = urls[0]
            checks.append(
                _check(
                    "Double-blind: URLs",
                    "to_check",
                    f"{len(urls)} URL(s), such as {text}; check none identifies the authors.",
                    file,
                    number,
                )
            )
    return checks


def _project_texts(entry: Path) -> dict[str, str]:
    root = entry.parent
    texts: dict[str, str] = {}
    for path in sorted(root.rglob("*.tex")):
        if any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        try:
            texts[path.relative_to(root).as_posix()] = _strip_comments(
                path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError:
            continue
    return texts


def check_venue(
    name: str,
    *,
    entry: Path,
    pdf: Path,
    page_count: int,
    roots: Iterable[Path] | None = None,
) -> dict[str, Any]:
    """Every rule of template ``name`` checked against the build's PDF and its sources."""
    found = find_template(name, list(roots) if roots is not None else session_template_roots())
    if found is None:
        return {
            "venue": name,
            "status": "failed",
            "checks": [
                _check("Template", "failed", f"No plugin in this session has a template {name!r}.")
            ],
        }
    _, template = found
    rules = template.get("rules") or {}
    checks = [*_page_checks(rules, pdf, page_count), *_source_checks(rules, _project_texts(entry))]
    status = "failed" if any(check["status"] == "failed" for check in checks) else "passed"
    return {
        "venue": name,
        "title": template.get("title") or name,
        "status": status,
        "checks": checks,
    }
