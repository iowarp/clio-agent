from __future__ import annotations

from pathlib import Path

from clio_agent.harness import extract_file_paths


def test_extract_file_paths_recognizes_genomics_suffixes(tmp_path: Path) -> None:
    fasta = tmp_path / "pathogen_reference.fasta"
    vcf = tmp_path / "pathogen_sample_variants.vcf"
    question = f"Review {fasta} and {vcf} for collaborator handoff."

    paths = extract_file_paths(question, "", {".fa", ".fasta", ".fna", ".vcf"})

    assert paths == [fasta, vcf]


def test_extract_file_paths_recognizes_new_scientific_domains(tmp_path: Path) -> None:
    cif = tmp_path / "sample.cif"
    geojson = tmp_path / "field_site.geojson"
    png = tmp_path / "microscopy.png"
    mzml = tmp_path / "run.mzML"
    question = f"Inspect {cif}, {geojson}, {png}, and {mzml}."

    paths = extract_file_paths(question, "", {".cif", ".geojson", ".png", ".mzml"})

    assert paths == [cif, geojson, png, mzml]
