"""Typed diagnostics from a Tectonic build (LaTeX plan, "Diagnostics"; ledger L4, L5, L5a, L5c).

Tectonic prints its own ``error: <file>:<line>: ...`` and ``warning: <file>:<line>: ...`` lines,
and writes the TeX ``.log`` and BibTeX's ``.blg`` with ``--keep-logs``. It does not always
write the ``.log`` (ledger L4), so its own lines come first and the logs add what only they
hold: undefined references and citations, and BibTeX's missing style files. Undefined keys
carry no file in the log, so each is located in the project's sources by its ``\\ref`` or
``\\cite``.

A build is failed when it has an ``error``, an undefined reference or citation, a missing
file or package, or an unsupported package, even when Tectonic exited cleanly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass

__all__ = [
    "FAILING_KINDS",
    "SHELL_ESCAPE_PACKAGES",
    "Diagnostic",
    "build_failed",
    "parse_diagnostics",
]

#: Packages that need shell escape, which builds never enable (ledger L5a).
SHELL_ESCAPE_PACKAGES = ("minted", "pythontex", "gnuplottex")

#: Kinds that make a build fail.
FAILING_KINDS = frozenset(
    {
        "error",
        "undefined_reference",
        "undefined_citation",
        "missing_file",
        "missing_package",
        "shell_escape_unsupported",
        "unsupported_bibliography_backend",
    }
)
# "warning" is a file TeX went on without (a hyphenation pattern the bundle could not send):
# reported, never failing.

# Files TeX loads from the bundle, as opposed to the project's own files.
_PACKAGE_SUFFIXES = (
    ".sty",
    ".cls",
    ".bst",
    ".bbx",
    ".cbx",
    ".def",
    ".fd",
    ".clo",
    ".cfg",
    ".tfm",
    ".otf",
    ".ttf",
    ".pfb",
    ".map",
    ".enc",
)

_LOCATED = re.compile(r"^(error|warning): (?P<file>[^\s:][^:]*):(?P<line>\d+): (?P<message>.*)$")
_PLAIN_ERROR = re.compile(r"^error: (?P<message>.*)$")
_FILE_NOT_FOUND = re.compile(r"File `(?P<name>[^']+)' not found")
_PICTURE = re.compile(r"Unable to load picture or PDF file '(?P<name>[^']+)'")
_OVERFULL = re.compile(r"Overfull \\[hv]box \((?P<amount>[\d.]+pt) too (?:wide|high)\)")
_UNDEFINED = re.compile(
    r"LaTeX Warning: (?P<what>Reference|Citation) `(?P<key>[^']+)' on page (?P<page>\d+) undefined"
)
_FONT = re.compile(r"(?:LaTeX )?Font Warning: (?P<message>.*)")
_BST = re.compile(r"I couldn't open style file (?P<name>\S+)")
_BIBER = "Running external tool biber"
_FETCH_FAILED = re.compile(r'failure fetching "(?P<name>[^"]+)" from network')
_BUNDLE_UNREACHABLE = "this bundle isn't cached, and we couldn't get it from the internet"
# Tectonic's summary and engine-noise lines, which carry no diagnosis of their own.
_NOISE = (
    "something bad happened inside XeTeX",
    "the XeTeX engine had an unrecoverable error",
    "warnings were issued by the TeX engine",
    "errors were issued by BibTeX",
    "Requested font",
    "open of input",
)
_MISSING_PACKAGE_MESSAGE = (
    "{name} could not be downloaded, so the build needs network access the first time "
    "this package is used."
)


@dataclass(frozen=True)
class Diagnostic:
    """One typed build finding, tied to a project file and line when the build said so."""

    kind: str
    message: str
    file: str = ""
    line: int = 0
    page: int = 0
    detail: str = ""

    def as_dict(self) -> dict[str, object]:
        """The diagnostic as plain data for tool results and the UI."""
        return {key: value for key, value in asdict(self).items() if value not in ("", 0)}


def _source_file(name: str, sources: Iterable[str]) -> str:
    """The project file Tectonic named, which drops the ``.tex`` of an ``\\input`` file."""
    known = set(sources)
    for candidate in (name, f"{name}.tex"):
        clean = candidate.removeprefix("./")
        if clean in known:
            return clean
    return name.removeprefix("./")


def _locate_key(key: str, command: str, texts: Mapping[str, str]) -> tuple[str, int]:
    """The first ``\\ref``-like or ``\\cite``-like use of ``key`` in the project's sources."""
    pattern = re.compile(
        r"\\(?:"
        + command
        + r")\*?(?:\[[^\]]*\])*\{[^}]*(?<![\w:.-])"
        + re.escape(key)
        + r"(?![\w:.-])[^}]*\}"
    )
    for name, text in texts.items():
        for number, line in enumerate(text.splitlines(), start=1):
            code = line.split("%", 1)[0]
            if pattern.search(code):
                return name, number
    return "", 0


def _referenced(name: str, texts: Mapping[str, str]) -> bool:
    """Whether the project's sources ask for ``name`` (a figure, an input, a data file)."""
    stem = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    return any(name in text or (stem and f"{{{stem}}}" in text) for text in texts.values())


def _missing(
    name: str,
    *,
    file: str,
    line: int,
    offline: bool,
    severity: str = "error",
    texts: Mapping[str, str] | None = None,
) -> Diagnostic:
    """A file TeX could not find: the project's, a package, or one TeX went on without."""
    sources = texts or {}
    if not name.lower().endswith(_PACKAGE_SUFFIXES) and (_referenced(name, sources) or not sources):
        return Diagnostic("missing_file", f"{name} is not in the project.", file, line, detail=name)
    if severity != "error":
        return Diagnostic(
            "warning", f"{name} was not found; TeX went on without it.", file, line, detail=name
        )
    stem = name.rsplit(".", 1)[0].lower()
    if stem in SHELL_ESCAPE_PACKAGES:
        # Fetching it would not help: the package runs programs, and builds never may.
        return Diagnostic(
            "shell_escape_unsupported",
            f"{stem} needs shell escape, which builds never enable.",
            file,
            line,
            detail=stem,
        )
    message = (
        _MISSING_PACKAGE_MESSAGE.format(name=name)
        if offline
        else f"{name} is not in the TeX bundle."
    )
    return Diagnostic("missing_package", message, file, line, detail=name)


def parse_diagnostics(
    output: str,
    *,
    log: str = "",
    blg: str = "",
    texts: Mapping[str, str] | None = None,
    offline: bool = False,
) -> list[Diagnostic]:
    """Typed diagnostics from Tectonic's output and logs.

    Args:
        output: Tectonic's standard output and error.
        log: the TeX ``.log`` when written.
        blg: BibTeX's ``.blg`` when written.
        texts: the project's text sources by project-relative path, for locating keys.
        offline: the build could not reach the bundle, so a missing package means network
            is needed rather than that the bundle lacks it.
    """
    sources = texts or {}
    # Tectonic says when the bundle's hosts were unreachable; a missing package then means
    # network is needed, not that the bundle lacks it (ledger L5c).
    offline = offline or _BUNDLE_UNREACHABLE in output or bool(_FETCH_FAILED.search(output))
    found: list[Diagnostic] = []
    seen: set[tuple[object, ...]] = set()

    def add(item: Diagnostic) -> None:
        # One entry per missing name, keeping the one that says where it was asked for.
        if item.kind in {"missing_package", "missing_file"} and item.detail:
            for index, existing in enumerate(found):
                if existing.kind == item.kind and existing.detail == item.detail:
                    if item.file and not existing.file:
                        found[index] = item
                    return
        key = (item.kind, item.file, item.line, item.detail or item.message)
        if key not in seen:
            seen.add(key)
            found.append(item)

    for raw in output.splitlines():
        line = raw.rstrip()
        if any(noise in line for noise in _NOISE):
            continue
        if _BUNDLE_UNREACHABLE in line:
            add(
                Diagnostic(
                    "missing_package",
                    "The TeX bundle is not cached and could not be downloaded, so the build "
                    "needs network access the first time it runs.",
                    detail="bundle",
                )
            )
            continue
        if _FETCH_FAILED.search(line):
            # Only marks the bundle unreachable; the file-not-found line that follows names
            # the file and where it was asked for.
            continue
        if _BIBER in line:
            add(
                Diagnostic(
                    "unsupported_bibliography_backend",
                    "This paper uses biblatex with biber, which this build does not ship. "
                    "Switch the paper to BibTeX (backend=bibtex or natbib).",
                    detail="biber",
                )
            )
            continue
        located = _LOCATED.match(line)
        if located:
            severity, message = located.group(1), located.group("message")
            file = _source_file(located.group("file"), sources)
            number = int(located.group("line"))
            message = message.removeprefix("! ").strip()
            if "-shell-escape" in message or "shell escape" in message.lower():
                package = next((p for p in SHELL_ESCAPE_PACKAGES if p in message), "")
                add(
                    Diagnostic(
                        "shell_escape_unsupported",
                        f"{package or 'This package'} needs shell escape, which builds never "
                        "enable.",
                        file,
                        number,
                        detail=package,
                    )
                )
                continue
            if missing := _FILE_NOT_FOUND.search(message) or _PICTURE.search(message):
                add(
                    _missing(
                        missing.group("name"),
                        file=file,
                        line=number,
                        offline=offline,
                        severity=severity,
                        texts=sources,
                    )
                )
                continue
            if overfull := _OVERFULL.search(message):
                add(
                    Diagnostic(
                        "overfull_box", message, file, number, detail=overfull.group("amount")
                    )
                )
                continue
            if font := _FONT.search(message):
                add(Diagnostic("font_warning", font.group("message"), file, number))
                continue
            if severity == "error":
                add(Diagnostic("error", message, file, number))
            continue
        plain = _PLAIN_ERROR.match(line)
        if plain and not plain.group("message").startswith("No such file or directory"):
            add(Diagnostic("error", plain.group("message").strip()))

    for raw in log.splitlines():
        if undefined := _UNDEFINED.search(raw):
            key, what = undefined.group("key"), undefined.group("what")
            command = (
                "ref|eqref|autoref|cref|Cref|pageref|nameref"
                if what == "Reference"
                else (r"cite[a-zA-Z]*|nocite")
            )
            file, number = _locate_key(key, command, sources)
            add(
                Diagnostic(
                    "undefined_reference" if what == "Reference" else "undefined_citation",
                    f"{what} {key} is undefined.",
                    file,
                    number,
                    page=int(undefined.group("page")),
                    detail=key,
                )
            )
        elif font := _FONT.search(raw):
            add(Diagnostic("font_warning", font.group("message").strip()))
    for raw in blg.splitlines():
        if style := _BST.search(raw):
            add(_missing(style.group("name"), file="", line=0, offline=offline, texts=sources))
    return found


def build_failed(diagnostics: Iterable[Diagnostic]) -> bool:
    """Whether these diagnostics make the build a failure (an error or a broken reference)."""
    return any(item.kind in FAILING_KINDS for item in diagnostics)
