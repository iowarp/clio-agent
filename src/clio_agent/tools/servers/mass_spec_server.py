"""Mass spectrometry mzML inspection tools for small XML fixtures."""

from __future__ import annotations

import csv
import math
from collections import Counter
from statistics import median
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


def _as_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"na", "nan", "null", "none", "inf", "-inf"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _split_prefixes(value: str) -> list[str]:
    return [item.strip().lower() for item in value.split(",") if item.strip()]


def _matches_any(text: str, prefixes: list[str]) -> bool:
    lowered = text.lower()
    return any(prefix in lowered for prefix in prefixes)


def _row_identifier(row: dict[str, str]) -> str:
    for key in (
        "Protein IDs",
        "Majority protein IDs",
        "Gene names",
        "Protein",
        "id",
    ):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return "unknown"


def _is_contaminant(row: dict[str, str]) -> bool:
    for key in ("Reverse", "Potential contaminant", "Only identified by site"):
        if str(row.get(key) or "").strip() == "+":
            return True
    protein_id = _row_identifier(row)
    return protein_id.startswith("CON__") or ";CON__" in protein_id


def _read_lfq_table(path) -> tuple[list[dict[str, str]], list[str], str]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        sample = handle.read(4096)
        handle.seek(0)
        delimiter = "\t" if sample.count("\t") >= sample.count(",") else ","
        reader = csv.DictReader(handle, delimiter=delimiter)
        rows = [dict(row) for row in reader]
        return rows, list(reader.fieldnames or []), delimiter


def _log2_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    return math.log2(value)


def _sample_medians(
    rows: list[dict[str, str]], sample_columns: list[str]
) -> dict[str, float]:
    medians: dict[str, float] = {}
    for column in sample_columns:
        values = [
            log_value
            for row in rows
            if (log_value := _log2_or_none(_as_float(row.get(column)))) is not None
        ]
        medians[column] = float(median(values)) if values else 0.0
    return medians


def _welch_score(group_a: list[float], group_b: list[float]) -> tuple[float, float]:
    if len(group_a) < 2 or len(group_b) < 2:
        return 0.0, 1.0
    mean_a = sum(group_a) / len(group_a)
    mean_b = sum(group_b) / len(group_b)
    var_a = sum((value - mean_a) ** 2 for value in group_a) / (len(group_a) - 1)
    var_b = sum((value - mean_b) ** 2 for value in group_b) / (len(group_b) - 1)
    denom = math.sqrt(var_a / len(group_a) + var_b / len(group_b))
    if denom == 0:
        return 0.0, 1.0
    score = (mean_b - mean_a) / denom
    # Normal approximation; sufficient for benchmark ranking without scipy.
    p_value = math.erfc(abs(score) / math.sqrt(2.0))
    return score, max(0.0, min(1.0, p_value))


def _bh_adjust(rows: list[dict[str, Any]]) -> None:
    ordered = sorted(enumerate(rows), key=lambda item: float(item[1]["p_value"]))
    total = len(ordered)
    previous = 1.0
    adjusted: dict[int, float] = {}
    for rank, (index, row) in reversed(list(enumerate(ordered, start=1))):
        p_value = float(row["p_value"])
        q_value = min(previous, p_value * total / rank)
        previous = q_value
        adjusted[index] = max(0.0, min(1.0, q_value))
    for index, row in enumerate(rows):
        row["adjusted_p_value"] = round(adjusted.get(index, 1.0), 6)


def _lfq_method_rows(
    *,
    rows: list[dict[str, str]],
    group_a_columns: list[str],
    group_b_columns: list[str],
    method: str,
) -> list[dict[str, Any]]:
    sample_columns = [*group_a_columns, *group_b_columns]
    medians = _sample_medians(rows, sample_columns) if method == "median" else {}
    out: list[dict[str, Any]] = []
    for row in rows:
        group_values: dict[str, list[float]] = {"a": [], "b": []}
        missing_by_group: dict[str, int] = {"a": 0, "b": 0}
        for group, columns in (("a", group_a_columns), ("b", group_b_columns)):
            for column in columns:
                value = _log2_or_none(_as_float(row.get(column)))
                if value is None:
                    missing_by_group[group] += 1
                    continue
                if method == "median":
                    value -= medians.get(column, 0.0)
                group_values[group].append(value)
        mean_a = (
            sum(group_values["a"]) / len(group_values["a"])
            if group_values["a"]
            else None
        )
        mean_b = (
            sum(group_values["b"]) / len(group_values["b"])
            if group_values["b"]
            else None
        )
        log2_fc = None if mean_a is None or mean_b is None else mean_b - mean_a
        t_score, p_value = _welch_score(group_values["a"], group_values["b"])
        out.append(
            {
                "protein": _row_identifier(row),
                "gene": str(row.get("Gene names") or row.get("Gene") or "").strip(),
                "group_a_observed": len(group_values["a"]),
                "group_b_observed": len(group_values["b"]),
                "group_a_missing": missing_by_group["a"],
                "group_b_missing": missing_by_group["b"],
                "group_a_mean_log2": None
                if mean_a is None
                else round(mean_a, 6),
                "group_b_mean_log2": None
                if mean_b is None
                else round(mean_b, 6),
                "log2_fold_change": None if log2_fc is None else round(log2_fc, 6),
                "t_score": round(t_score, 6),
                "p_value": round(p_value, 6),
            }
        )
    _bh_adjust(out)
    return out


def _method_quality(
    rows: list[dict[str, Any]],
    spike_terms: list[str],
    expected_spike_log2fc: float | None,
) -> dict[str, Any]:
    spike_rows = [
        row
        for row in rows
        if spike_terms
        and _matches_any(f"{row.get('protein', '')} {row.get('gene', '')}", spike_terms)
        and row.get("log2_fold_change") is not None
    ]
    background_rows = [
        row
        for row in rows
        if row.get("log2_fold_change") is not None and row not in spike_rows
    ]
    background_abs = [
        abs(float(row["log2_fold_change"])) for row in background_rows
    ]
    spike_fcs = [float(row["log2_fold_change"]) for row in spike_rows]
    background_median_abs = float(median(background_abs)) if background_abs else 0.0
    spike_median = float(median(spike_fcs)) if spike_fcs else None
    spike_error = (
        None
        if spike_median is None or expected_spike_log2fc is None
        else abs(spike_median - expected_spike_log2fc)
    )
    quality_score = background_median_abs + (spike_error if spike_error is not None else 0.0)
    return {
        "background_median_abs_log2fc": round(background_median_abs, 6),
        "spike_count": len(spike_rows),
        "spike_median_log2fc": None if spike_median is None else round(spike_median, 6),
        "spike_expected_log2fc": expected_spike_log2fc,
        "spike_abs_error": None if spike_error is None else round(spike_error, 6),
        "quality_score": round(quality_score, 6),
    }


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


@mass_spec_server.tool()
def lfq_differential_abundance(
    filepath: str,
    group_a_prefix: str,
    group_b_prefix: str,
    spike_terms: str = "",
    expected_spike_log2fc: float | None = None,
    min_observed_per_group: int = 2,
    max_proteins: int = 50,
) -> dict[str, Any]:
    """Analyze an LFQ protein-intensity matrix for two-condition abundance shifts.

    Agent story: Use this for MaxQuant-style proteinGroups tables or compact
    LFQ matrices when the user asks which proteins change between conditions.
    It compares raw and median-normalized log2 intensities, filters common
    contaminant/reverse rows, reports missingness, and returns a ranked
    differential-abundance table.

    Args:
        filepath: Path to a TSV/CSV LFQ intensity matrix.
        group_a_prefix: Comma-separated column-name fragments for condition A.
        group_b_prefix: Comma-separated column-name fragments for condition B.
        spike_terms: Optional comma-separated protein/gene fragments for known
            spike-ins used to evaluate normalization quality.
        expected_spike_log2fc: Optional expected median spike-in log2 fold
            change for normalization quality scoring.
        min_observed_per_group: Minimum observed samples per group for ranking.
        max_proteins: Maximum ranked proteins returned.

    Returns:
        Dictionary with selected normalization, method quality, filtered row
        counts, sample columns, missingness, and ranked protein changes.
    """
    try:
        safe_path = validate_read_path(filepath)
        rows, fieldnames, delimiter = _read_lfq_table(safe_path)
        if not rows or not fieldnames:
            return {"ok": False, "filepath": str(safe_path), "error": "LFQ table is empty"}

        group_a_terms = _split_prefixes(group_a_prefix)
        group_b_terms = _split_prefixes(group_b_prefix)
        if not group_a_terms or not group_b_terms:
            return {
                "ok": False,
                "filepath": str(safe_path),
                "error": "Both group_a_prefix and group_b_prefix are required",
            }

        numeric_columns = [
            column
            for column in fieldnames
            if any(_as_float(row.get(column)) is not None for row in rows[: min(len(rows), 100)])
        ]
        group_a_columns = [column for column in numeric_columns if _matches_any(column, group_a_terms)]
        group_b_columns = [column for column in numeric_columns if _matches_any(column, group_b_terms)]
        if not group_a_columns or not group_b_columns:
            return {
                "ok": False,
                "filepath": str(safe_path),
                "error": "Could not find intensity columns for both requested groups",
                "available_numeric_columns": numeric_columns[:50],
            }

        filtered_rows = [row for row in rows if not _is_contaminant(row)]
        removed_rows = len(rows) - len(filtered_rows)
        min_observed_per_group = max(1, int(min_observed_per_group or 2))
        max_proteins = max(1, min(int(max_proteins or 50), 500))
        spike_tokens = _split_prefixes(spike_terms)

        method_results: dict[str, list[dict[str, Any]]] = {}
        qualities: dict[str, dict[str, Any]] = {}
        for method in ("raw", "median"):
            method_rows = _lfq_method_rows(
                rows=filtered_rows,
                group_a_columns=group_a_columns,
                group_b_columns=group_b_columns,
                method=method,
            )
            method_results[method] = method_rows
            qualities[method] = _method_quality(
                method_rows,
                spike_tokens,
                expected_spike_log2fc,
            )
        selected_method = min(
            qualities,
            key=lambda method: float(qualities[method]["quality_score"]),
        )

        ranked = [
            row
            for row in method_results[selected_method]
            if row.get("log2_fold_change") is not None
            and int(row["group_a_observed"]) >= min_observed_per_group
            and int(row["group_b_observed"]) >= min_observed_per_group
        ]
        ranked.sort(
            key=lambda row: (
                float(row["adjusted_p_value"]),
                -abs(float(row["log2_fold_change"])),
            )
        )
        sample_missingness = {}
        for column in [*group_a_columns, *group_b_columns]:
            missing = sum(1 for row in filtered_rows if _as_float(row.get(column)) is None)
            sample_missingness[column] = {
                "missing": missing,
                "observed": len(filtered_rows) - missing,
                "missing_fraction": round(missing / len(filtered_rows), 6)
                if filtered_rows
                else 0.0,
            }

        return {
            "ok": True,
            "filepath": str(safe_path),
            "delimiter": "tab" if delimiter == "\t" else delimiter,
            "input_rows": len(rows),
            "analyzed_rows": len(filtered_rows),
            "removed_contaminant_or_reverse_rows": removed_rows,
            "group_a_columns": group_a_columns,
            "group_b_columns": group_b_columns,
            "sample_missingness": sample_missingness,
            "normalization_methods": qualities,
            "selected_normalization": selected_method,
            "ranked_proteins": ranked[:max_proteins],
            "ranked_proteins_truncated": len(ranked) > max_proteins,
            "ranking_policy": {
                "min_observed_per_group": min_observed_per_group,
                "sort": "adjusted_p_value_then_abs_log2_fold_change",
            },
        }
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return {"error": str(exc)}
