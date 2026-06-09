"""Structural tests for HDF5ExpertSignature.

These guard against future drift between the signature's skill index and
the bundled SKILL.md library. If a skill is added or removed, the index
must be updated in lockstep so the LLM is never told a skill exists that
it can't actually consult.
"""

from clio_agent.experts.hdf5_skills import skill_names
from clio_agent.signatures import HDF5ExpertSignature


def test_signature_has_domain_prompt():
    """Substantial domain prompt (500+ words), matching the convention for
    DataExpertSignature / AnalysisExpertSignature / VisualizationSignature."""
    assert HDF5ExpertSignature.__doc__ is not None
    word_count = len(HDF5ExpertSignature.__doc__.split())
    assert word_count >= 500, f"signature prompt is only {word_count} words, need 500+"


def test_signature_docstring_under_budget():
    """Stays under ~800 words so it doesn't crowd the tier-2 token budget."""
    assert HDF5ExpertSignature.__doc__ is not None
    word_count = len(HDF5ExpertSignature.__doc__.split())
    assert word_count < 800, f"docstring is {word_count} words — trim it"


def test_signature_indexes_every_bundled_skill():
    doc = HDF5ExpertSignature.__doc__ or ""
    missing = [name for name in skill_names() if name not in doc]
    assert not missing, f"signature index is missing skills: {missing}"


def test_signature_input_output_shape():
    fields = HDF5ExpertSignature.fields
    assert set(fields) == {"question", "file_context", "analysis", "recommendations"}
