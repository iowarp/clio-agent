"""The resolver registry: how each kind of document answers "what is under this mark?".

A resolver does two things for the anchors of one document profile (comments plan, "One
anchor model"): it resolves an anchor against an artifact version into what the agent is told
(readable lines, the text under the mark, a crop, and where the mark sits for the overview),
and it re-anchors an anchor onto a newer version, saying whether the anchor moved or is now
outdated. The LaTeX plan registers its ``source-map`` resolver here (``register``).

Resolvers are looked up by the document's profile and the anchor's profile, so one document
can mix them: an HTML artifact resolves a text quote with the text resolver and a region or a
picked element with the HTML resolver.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from clio_agent.gact.documents.models import AnchorProfile, DocumentAnchor, DocumentProfile

if TYPE_CHECKING:
    from PIL import Image

    from clio_agent.gact.artifacts.records import ArtifactRecord, ArtifactVersion

__all__ = [
    "AnchorState",
    "MarkMode",
    "MarkTarget",
    "ResolvedMark",
    "Resolver",
    "mark_mode",
    "register",
    "resolver_for",
]

MarkMode = Literal["text", "region", "element", "native"]
AnchorState = Literal["current", "moved", "outdated"]

_MODES: dict[str, MarkMode] = {
    "text-quote": "text",
    "pdf-quad": "text",
    "sheet-range": "text",
    "slide-shape": "text",
    "source-map": "text",
    "region": "region",
    "dom": "element",
    "native-comment": "native",
}


def mark_mode(anchor: DocumentAnchor) -> MarkMode:
    """The comment mode an anchor was made in: text, region, element or a native comment."""
    return _MODES[anchor.profile]


@dataclass
class MarkTarget:
    """One artifact version a mark is resolved against, and what the person saw of it.

    ``view_path`` is the PDF rendition when the person commented on an Office file's
    rendition; otherwise it is the version's own bytes. ``cache`` holds what a resolver
    computed for this version (rasterized pages, a render) so a session of several marks
    computes it once.
    """

    app: Any
    record: "ArtifactRecord"
    version: "ArtifactVersion"
    path: Path
    profile: DocumentProfile
    view_path: Path
    session_id: str = ""
    cache: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        """The artifact's name, as the entry shows it."""
        return self.record.name

    @property
    def via_rendition(self) -> bool:
        """Whether the person commented on a PDF rendition of the source."""
        return self.view_path != self.path


@dataclass
class ResolvedMark:
    """What one mark resolves to, before the renderer cuts and numbers it.

    ``lines`` are the entry's readable lines in order, as (label, value), such as
    ("Selected", '"..."') or ("Element", "<button id=...>"). ``under`` is everything that lies
    under the mark, uncut. ``page_index`` and ``rect`` place the mark on its page (or the whole
    image) for the overview; ``crop`` is the area under a region or element. ``notes`` are
    typed reasons a part could not be resolved, stated in the entry rather than dropped.
    """

    mode: MarkMode
    lines: list[tuple[str, str]] = field(default_factory=list)
    under: str = ""
    crop: bytes | None = None
    page_index: int | None = None
    rect: tuple[float, float, float, float] | None = None
    notes: list[str] = field(default_factory=list)


class Resolver(Protocol):
    """Resolve anchors of some document profiles, and carry them to newer versions."""

    def resolve(self, target: MarkTarget, anchor: DocumentAnchor) -> ResolvedMark:
        """What the agent is told about ``anchor`` on ``target``."""
        ...

    def page_image(self, target: MarkTarget, page_index: int | None) -> "Image.Image | None":
        """The page (or the whole image) the overview draws marks on; ``None`` when this
        kind of document has no picture (a Markdown or LaTeX source)."""
        ...

    def reanchor(
        self, old: MarkTarget, new: MarkTarget, anchor: DocumentAnchor
    ) -> tuple[AnchorState, DocumentAnchor]:
        """``anchor`` carried onto ``new``: moved with its new position, or outdated."""
        ...


_LOCK = threading.Lock()
_LOAD_LOCK = threading.Lock()
_REGISTRY: dict[tuple[DocumentProfile, AnchorProfile], Resolver] = {}
_BUILTINS_LOADED = False


def register(
    profiles: Iterable[DocumentProfile],
    anchor_profiles: Iterable[AnchorProfile],
    resolver: Resolver,
) -> None:
    """Register ``resolver`` for every (document profile, anchor profile) pair given.

    A later registration for the same pair replaces the earlier one, which is how the
    LaTeX plan's ``source-map`` resolver takes over LaTeX PDFs once it lands.
    """

    with _LOCK:
        for profile in profiles:
            for anchor_profile in anchor_profiles:
                _REGISTRY[(profile, anchor_profile)] = resolver


def resolver_for(profile: DocumentProfile, anchor_profile: AnchorProfile) -> Resolver | None:
    """The resolver for an anchor of ``anchor_profile`` on a document of ``profile``."""

    _load_builtins()
    with _LOCK:
        return _REGISTRY.get((profile, anchor_profile))


def _load_builtins() -> None:
    """Register the built-in resolvers once, before any lookup can see the registry."""

    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    with _LOAD_LOCK:
        if _BUILTINS_LOADED:
            return
        from clio_agent.gact.documents import (  # noqa: PLC0415 - avoid an import cycle
            anchor_html,
            anchor_image,
            anchor_pdf,
            anchor_text,
        )

        for load in (
            anchor_text.register_builtin,
            anchor_pdf.register_builtin,
            anchor_image.register_builtin,
            anchor_html.register_builtin,
        ):
            load()
        _BUILTINS_LOADED = True
