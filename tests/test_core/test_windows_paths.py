"""Windows-correctness regressions for issue #765.

Covers the three hand-synced scientific-suffix vocabularies (agent set,
harness regex, gact/delegation evidence-index patterns), which must derive
from the single shared module ``clio_agent.scientific_suffixes``.

Note: ``ClioAgent._local_paths_from_value`` and its Windows-path tests were
deleted with the tier-2 expert arm (issue #768) — the helper's only caller
died with that arm. Windows path grounding lives on in the shared
``clio_agent.scientific_suffixes`` vocabulary and
``harness.extract_file_paths`` (``WINDOWS_FILE_PATH_RE``), exercised below.
"""

from __future__ import annotations

from clio_agent import harness
from clio_agent.agent import SCIENTIFIC_FILE_SUFFIXES as AGENT_SUFFIXES


class TestSuffixVocabularyConsolidation:
    """One shared constant module feeds every suffix vocabulary (issue #765 (a))."""

    def test_shared_module_is_single_source(self) -> None:
        from clio_agent.scientific_suffixes import SCIENTIFIC_FILE_SUFFIXES

        assert AGENT_SUFFIXES == SCIENTIFIC_FILE_SUFFIXES

    def test_harness_pattern_derives_from_shared_vocabulary(self) -> None:
        from clio_agent.scientific_suffixes import scientific_suffix_alternation

        assert harness.SCIENTIFIC_PATH_SUFFIX_PATTERN == scientific_suffix_alternation()

    def test_every_shared_suffix_matched_by_harness_regexes(self) -> None:
        from clio_agent.scientific_suffixes import SCIENTIFIC_FILE_SUFFIXES

        for suffix in SCIENTIFIC_FILE_SUFFIXES:
            windows = rf"C:\data\sample{suffix}"
            posix = f"/data/sample{suffix}"
            assert harness.WINDOWS_FILE_PATH_RE.search(windows), suffix
            assert harness.ROOTED_FILE_PATH_RE.search(posix), suffix

    def test_delegation_evidence_index_uses_shared_vocabulary(self) -> None:
        from clio_agent.gact.delegation import _compact_exact_evidence_index

        # mzml/geojson are in the shared vocabulary but were missing from the
        # hand-synced inline copy in gact/delegation.py.
        transcript = (
            "Staged D:\\staging\\proteomics\\run_a.mzml and "
            "/data/sites/field_area.geojson for review. "
            "Config lives at /etc/clio/settings.json."
        )
        lines = _compact_exact_evidence_index(transcript).splitlines()
        assert "- D:\\staging\\proteomics\\run_a.mzml" in lines
        assert "- /data/sites/field_area.geojson" in lines
        # json stays a delegation-local extension (evidence indexing only).
        assert "- /etc/clio/settings.json" in lines
