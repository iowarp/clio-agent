"""What Settings shows about each search source: what it is, its free use, its key page.

Figures are the services' own published terms as of November 2026; an empty
``free_use`` means the service publishes no number. They are shown as reference
text, never used to decide anything.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from clio_agent.search.settings import DISPLAY_NAMES, KEY_REQUIRED


@dataclass(frozen=True)
class SourceInfo:
    """Settings text for one source."""

    id: str
    description: str
    free_use: str = ""
    key_page: str = ""
    key_benefit: str = ""
    reads_pages: bool = False

    def as_dict(self) -> dict[str, object]:
        """The row the settings route returns (plus its name and key need)."""

        row: dict[str, object] = asdict(self)
        row["name"] = DISPLAY_NAMES.get(self.id, self.id)
        row["key_required"] = self.id in KEY_REQUIRED
        return row


WEB_INFO: dict[str, SourceInfo] = {
    "keenable": SourceInfo(
        "keenable",
        "Its own web index; filters by date, site and excluded sites.",
        "1,000 searches per hour, shared by everyone on your network",
        "https://keenable.ai/signup",
        "A private allowance (100,000 requests a month free).",
        reads_pages=True,
    ),
    "exa": SourceInfo(
        "exa",
        "Its own web index; filters by date, site and kind (papers, news, PDF).",
        "",
        "https://dashboard.exa.ai/api-keys",
        "Your own monthly allowance, and deeper search modes.",
        reads_pages=True,
    ),
    "firecrawl": SourceInfo(
        "firecrawl",
        "Web search with research and PDF categories and full page text.",
        "",
        "https://firecrawl.dev",
        "1,000 credits a month on a free key.",
    ),
    "you": SourceInfo(
        "you",
        "Its own web index; filters by date and excluded sites.",
        "100 searches per day, shared by everyone on your network",
        "https://you.com/platform",
        "A private allowance and site allowlists.",
    ),
    "parallel": SourceInfo(
        "parallel",
        "Web search for agents; the free tier is its faster, lighter mode.",
        "",
        "https://platform.parallel.ai",
        "Higher-quality search mode and date and site filters.",
        reads_pages=True,
    ),
    "service": SourceInfo(
        "service",
        "A CLIO search service your site runs (search.service.url).",
        "Set by the service",
        "",
        "The service's access token, if it needs one.",
    ),
}

LITERATURE_INFO: dict[str, SourceInfo] = {
    "openalex": SourceInfo(
        "openalex",
        "Works across every field, with citations and open-access links.",
        "About 100 searches a day per network without a key; single-work lookups are free",
        "https://openalex.org",
        "A free key ($1 a day budget); CLIO can request one with your email.",
    ),
    "crossref": SourceInfo(
        "crossref",
        "DOI metadata for journals, preprints and their links.",
        "No daily limit; paced",
    ),
    "chemrxiv": SourceInfo(
        "chemrxiv",
        "Chemistry preprints, found through Crossref (ChemRxiv's DOI prefix).",
        "No daily limit; shares Crossref's pace",
    ),
    "arxiv": SourceInfo(
        "arxiv",
        "Preprints in physics, computing, mathematics and more.",
        "One request every 3 seconds",
    ),
    "europepmc": SourceInfo(
        "europepmc", "Life-science literature and preprints, with full text where open.", "No key"
    ),
    "osti": SourceInfo("osti", "U.S. Department of Energy reports and papers.", "No key"),
    "opencitations": SourceInfo(
        "opencitations", "Open citation links: what a work cites and who cites it.", "No key"
    ),
    "datacite": SourceInfo("datacite", "DOIs for datasets and software.", "No key"),
    "semantic_scholar": SourceInfo(
        "semantic_scholar",
        "Papers across fields with citation data.",
        "Needs your key",
        "https://www.semanticscholar.org/product/api#api-key-form",
        "Turns the source on.",
    ),
}

__all__ = ["LITERATURE_INFO", "WEB_INFO", "SourceInfo"]
