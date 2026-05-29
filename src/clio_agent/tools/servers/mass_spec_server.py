"""Mass spectrometry mzML inspection tools for small XML fixtures."""

from __future__ import annotations

from collections import Counter
from typing import Any
from xml.etree import ElementTree

from fastmcp import FastMCP

from clio_agent.tools.file_policy import FilePolicyError, validate_read_path

mass_spec_server = FastMCP("mass_spec")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _cv_name(element: ElementTree.Element) -> str:
    return str(element.attrib.get("name") or element.attrib.get("accession") or "")


def _parse_numbers(text: str | None) -> list[float]:
    if not text:
        return []
    values: list[float] = []
    for item in text.replace(",", " ").split():
        try:
            values.append(float(item))
        except ValueError:
            continue
    return values


def _spectrum_arrays(spectrum: ElementTree.Element) -> tuple[list[float], list[float]]:
    mz_values: list[float] = []
    intensity_values: list[float] = []
    for child in spectrum.iter():
        if _local_name(child.tag) != "binaryDataArray":
            continue
        names = {
            _cv_name(grandchild).lower()
            for grandchild in child
            if _local_name(grandchild.tag) == "cvParam"
        }
        binary = next(
            (
                grandchild
                for grandchild in child
                if _local_name(grandchild.tag) == "binary"
            ),
            None,
        )
        values = _parse_numbers(binary.text if binary is not None else "")
        if "m/z array" in names:
            mz_values = values
        elif "intensity array" in names:
            intensity_values = values
    return mz_values, intensity_values


@mass_spec_server.tool()
def inspect_mzml(filepath: str, max_spectra: int = 12) -> dict[str, Any]:
    """Inspect a small mzML file for spectra, MS levels, peaks, and intensity totals.

    Agent story: Use this when a user asks whether mass spectrometry data is
    ready for collaborator review, peptide search, QC, or downstream analysis.

    Args:
        filepath: Path to an mzML XML file.
        max_spectra: Maximum representative spectra to include.

    Returns:
        Dictionary with spectrum counts, MS-level distribution, scan summaries,
        m/z range, total-ion-current style intensity totals, and parser warnings.
    """
    try:
        safe_path = validate_read_path(filepath)
        max_spectra = max(1, min(int(max_spectra or 12), 100))
        root = ElementTree.parse(safe_path).getroot()

        spectra = [
            element
            for element in root.iter()
            if _local_name(element.tag) == "spectrum"
        ]
        ms_levels: Counter[str] = Counter()
        total_peaks = 0
        mz_min: float | None = None
        mz_max: float | None = None
        tic_values: list[float] = []
        summaries: list[dict[str, Any]] = []
        warnings: list[str] = []

        for index, spectrum in enumerate(spectra):
            spectrum_id = str(spectrum.attrib.get("id") or f"spectrum_{index}")
            ms_level = ""
            declared_peaks = int(spectrum.attrib.get("defaultArrayLength") or 0)
            scan_start_time = ""
            for child in spectrum.iter():
                if _local_name(child.tag) != "cvParam":
                    continue
                name = _cv_name(child)
                if name == "ms level":
                    ms_level = str(child.attrib.get("value") or "")
                elif name == "scan start time":
                    scan_start_time = str(child.attrib.get("value") or "")
                elif name == "total ion current":
                    try:
                        tic_values.append(float(str(child.attrib.get("value") or "0")))
                    except ValueError:
                        warnings.append(f"{spectrum_id}: invalid total ion current")

            mz_values, intensity_values = _spectrum_arrays(spectrum)
            if not ms_level:
                ms_level = "unknown"
            ms_levels[ms_level] += 1
            peak_count = max(len(mz_values), len(intensity_values), declared_peaks)
            total_peaks += peak_count
            if mz_values:
                local_min = min(mz_values)
                local_max = max(mz_values)
                mz_min = local_min if mz_min is None else min(mz_min, local_min)
                mz_max = local_max if mz_max is None else max(mz_max, local_max)
            if intensity_values and len(intensity_values) != len(mz_values):
                warnings.append(f"{spectrum_id}: m/z and intensity arrays differ in length")
            if index < max_spectra:
                summaries.append(
                    {
                        "id": spectrum_id,
                        "ms_level": ms_level,
                        "scan_start_time": scan_start_time,
                        "peak_count": peak_count,
                        "tic": tic_values[-1] if tic_values else None,
                    }
                )

        return {
            "filepath": str(safe_path),
            "format": "mzML",
            "spectrum_count": len(spectra),
            "ms_levels": dict(ms_levels),
            "total_peak_count": total_peaks,
            "mz_range": [] if mz_min is None or mz_max is None else [mz_min, mz_max],
            "tic_total": float(round(sum(tic_values), 3)),
            "tic_max": float(round(max(tic_values), 3)) if tic_values else 0.0,
            "total_ion_current_total": float(round(sum(tic_values), 3)),
            "total_ion_current_max": float(round(max(tic_values), 3)) if tic_values else 0.0,
            "representative_spectra": summaries,
            "spectra_truncated": len(spectra) > max_spectra,
            "warnings": warnings,
            "ok": True,
        }
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return {"error": str(exc)}
