"""Windows-correctness regressions for issue #765.

Covers:
- ``ClioAgent._local_paths_from_value`` must recognize Windows drive paths
  (``D:\\...`` and ``D:/...``) so staged files reach the planner's
  ``local_paths`` grounding on Windows.
- The three hand-synced scientific-suffix vocabularies (agent set, harness
  regex, gact/delegation evidence-index patterns) must derive from the single
  shared module ``clio_agent.scientific_suffixes``.
- The artifact-root fallback must use ``tempfile.gettempdir()``, not a POSIX
  ``/tmp`` literal.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from clio_agent import harness
from clio_agent.agent import SCIENTIFIC_FILE_SUFFIXES as AGENT_SUFFIXES
from clio_agent.agent import ClioAgent


class TestLocalPathsFromValueWindows:
    """Staged Windows paths must reach the planner grounding (issue #765 (a))."""

    def test_backslash_drive_path_in_string(self) -> None:
        value = r"Staged the station table at D:\staging\earthscope\data.csv for analysis."
        assert ClioAgent._local_paths_from_value(value) == [r"D:\staging\earthscope\data.csv"]

    def test_forward_slash_drive_path_in_string(self) -> None:
        value = "wrote D:/staging/output.parquet next"
        assert ClioAgent._local_paths_from_value(value) == ["D:/staging/output.parquet"]

    def test_windows_path_in_nested_payload(self) -> None:
        payload = {
            "result": {
                "files": [r"C:\Users\alice\run.h5", "notes"],
                "detail": "no path here",
            }
        }
        assert ClioAgent._local_paths_from_value(payload) == [r"C:\Users\alice\run.h5"]

    def test_posix_paths_still_extracted(self) -> None:
        value = "outputs: /data/exp/results.parquet and /tmp/scratch/plot.png"
        assert ClioAgent._local_paths_from_value(value) == [
            "/data/exp/results.parquet",
            "/tmp/scratch/plot.png",
        ]

    def test_non_scientific_suffix_ignored(self) -> None:
        value = r"see D:\staging\notes.txt and /var/log/app.log"
        assert ClioAgent._local_paths_from_value(value) == []


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


class TestArtifactRootFallback:
    """Artifact fallback must use the platform temp dir (issue #765 (b))."""

    def test_default_artifact_root_uses_platform_tempdir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLIO_ARTIFACT_DIR", raising=False)
        monkeypatch.delenv("CLIO_ALLOWED_ROOTS", raising=False)
        root = ClioAgent._default_artifact_root(Path("plot.png"))
        assert root == Path(tempfile.gettempdir()) / "clio-agent-artifacts"
