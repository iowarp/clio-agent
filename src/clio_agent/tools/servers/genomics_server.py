"""Genomics file inspection tools for small FASTA and VCF fixtures."""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastmcp import FastMCP

from clio_agent.tools.file_policy import FilePolicyError, validate_read_path

genomics_server = FastMCP("genomics")


def _gc_fraction(sequence: str) -> float:
    """Return GC fraction over non-ambiguous bases."""
    counts = Counter(base for base in sequence.upper() if base in {"A", "C", "G", "T"})
    total = sum(counts.values())
    if total == 0:
        return 0.0
    return round((counts["G"] + counts["C"]) / total, 6)


def _variant_type(ref: str, alt: str) -> str:
    """Classify one REF/ALT allele pair."""
    if len(ref) == 1 and len(alt) == 1:
        return "snp"
    if len(ref) < len(alt):
        return "insertion"
    if len(ref) > len(alt):
        return "deletion"
    return "substitution"


def _parse_info(info: str) -> dict[str, str | bool]:
    """Parse a compact VCF INFO field."""
    parsed: dict[str, str | bool] = {}
    if not info or info == ".":
        return parsed
    for item in info.split(";"):
        if not item:
            continue
        if "=" not in item:
            parsed[item] = True
            continue
        key, value = item.split("=", 1)
        parsed[key] = value
    return parsed


@genomics_server.tool()
def inspect_fasta(filepath: str, max_records: int = 20) -> dict[str, Any]:
    """Inspect a FASTA file: record count, lengths, GC content, and base counts.

    Agent story: Use this before interpreting variants or comparing genomic
    samples so the answer is grounded in the actual reference sequence content.

    Args:
        filepath: Path to a FASTA file.
        max_records: Maximum number of sequence summaries to include.

    Returns:
        Dictionary with record summaries, total bases, base composition, GC
        fraction, and longest-record metadata.
    """
    try:
        safe_path = validate_read_path(filepath)
        max_records = max(1, min(int(max_records or 20), 100))
        records: list[dict[str, Any]] = []
        current_id = ""
        current_desc = ""
        chunks: list[str] = []

        def flush_record() -> None:
            nonlocal chunks, current_id, current_desc
            if not current_id:
                return
            sequence = "".join(chunks).upper()
            counts = Counter(sequence)
            record = {
                "id": current_id,
                "description": current_desc,
                "length": len(sequence),
                "gc_fraction": _gc_fraction(sequence),
                "ambiguous_bases": sum(counts[base] for base in counts if base not in "ACGT"),
                "base_counts": {base: counts.get(base, 0) for base in ("A", "C", "G", "T", "N")},
            }
            records.append(record)
            chunks = []

        with safe_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                if line.startswith(">"):
                    flush_record()
                    header = line[1:].strip()
                    bits = header.split(maxsplit=1)
                    current_id = bits[0] if bits else ""
                    current_desc = bits[1] if len(bits) > 1 else ""
                    continue
                chunks.append(line)
        flush_record()

        if not records:
            return {"error": f"No FASTA records found in {safe_path}"}

        total_bases = sum(record["length"] for record in records)
        total_counts: Counter[str] = Counter()
        for record in records:
            total_counts.update(record["base_counts"])
        longest = max(records, key=lambda record: int(record["length"]))
        returned_records = sorted(records, key=lambda record: int(record["length"]), reverse=True)[
            :max_records
        ]
        return {
            "filepath": str(safe_path),
            "record_count": len(records),
            "total_bases": total_bases,
            "gc_fraction": _gc_fraction(
                "".join(base * count for base, count in total_counts.items() if base in "ACGT")
            ),
            "base_counts": {base: total_counts.get(base, 0) for base in ("A", "C", "G", "T", "N")},
            "longest_record": {
                "id": longest["id"],
                "length": longest["length"],
                "gc_fraction": longest["gc_fraction"],
            },
            "records": returned_records,
            "records_truncated": len(returned_records) < len(records),
            "ok": True,
        }
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return {"error": str(exc)}


@genomics_server.tool()
def summarize_vcf(filepath: str, max_variants: int = 25) -> dict[str, Any]:
    """Summarize a VCF file: samples, variant types, filters, and high-impact rows.

    Agent story: Use this when a user asks which variants are present, whether
    a variant file looks review-ready, or what genomic changes deserve follow-up.

    Args:
        filepath: Path to a VCF file.
        max_variants: Maximum representative variant rows to include.

    Returns:
        Dictionary with sample IDs, variant counts by type/chrom/filter, and
        representative rows including INFO annotations.
    """
    try:
        safe_path = validate_read_path(filepath)
        max_variants = max(1, min(int(max_variants or 25), 200))
        samples: list[str] = []
        variant_rows: list[dict[str, Any]] = []
        type_counts: Counter[str] = Counter()
        chrom_counts: Counter[str] = Counter()
        filter_counts: Counter[str] = Counter()
        effect_counts: Counter[str] = Counter()

        with safe_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                if line.startswith("##"):
                    continue
                if line.startswith("#CHROM"):
                    columns = line.split("\t")
                    samples = columns[9:]
                    continue
                columns = line.split("\t")
                if len(columns) < 8:
                    continue
                chrom, pos, var_id, ref, alt_field, qual, filter_value, info_field = columns[:8]
                alts = alt_field.split(",")
                info = _parse_info(info_field)
                effects = str(info.get("EFFECT") or info.get("ANN") or "unannotated")
                for effect in effects.split(","):
                    effect_counts[effect] += 1
                for alt in alts:
                    type_counts[_variant_type(ref, alt)] += 1
                chrom_counts[chrom] += 1
                filter_counts[filter_value or "."] += 1
                row: dict[str, Any] = {
                    "chrom": chrom,
                    "pos": int(pos) if pos.isdigit() else pos,
                    "id": var_id,
                    "ref": ref,
                    "alt": alts,
                    "qual": None if qual == "." else float(qual),
                    "filter": filter_value,
                    "type": sorted({_variant_type(ref, alt) for alt in alts}),
                    "info": info,
                }
                if len(columns) > 9 and samples:
                    row["sample_values"] = dict(zip(samples, columns[9:], strict=False))
                variant_rows.append(row)

        return {
            "filepath": str(safe_path),
            "sample_count": len(samples),
            "samples": samples,
            "variant_count": len(variant_rows),
            "variant_types": dict(type_counts),
            "chromosomes": dict(chrom_counts),
            "filters": dict(filter_counts),
            "effects": dict(effect_counts),
            "representative_variants": variant_rows[:max_variants],
            "variants_truncated": len(variant_rows) > max_variants,
            "ok": True,
        }
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return {"error": str(exc)}
