"""Materials science file inspection tools for small CIF fixtures."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any

from fastmcp import FastMCP

from clio_agent.tools.file_policy import FilePolicyError, validate_read_path

materials_server = FastMCP("materials")

ATOMIC_WEIGHTS = {
    "C": 12.011,
    "O": 15.999,
    "Sr": 87.62,
    "Ti": 47.867,
}


def _clean_value(value: str) -> str:
    """Remove common CIF quoting and uncertainty notation."""
    cleaned = value.strip().strip("'\"")
    return re.sub(r"\([0-9]+\)$", "", cleaned)


def _to_float(value: str, default: float = 0.0) -> float:
    try:
        return float(_clean_value(value))
    except ValueError:
        return default


def _cell_volume(cell: dict[str, float]) -> float:
    """Return unit-cell volume in cubic Angstroms."""
    a = cell.get("a", 0.0)
    b = cell.get("b", 0.0)
    c = cell.get("c", 0.0)
    alpha = math.radians(cell.get("alpha", 90.0))
    beta = math.radians(cell.get("beta", 90.0))
    gamma = math.radians(cell.get("gamma", 90.0))
    factor = math.sqrt(
        max(
            0.0,
            1.0
            - math.cos(alpha) ** 2
            - math.cos(beta) ** 2
            - math.cos(gamma) ** 2
            + 2.0 * math.cos(alpha) * math.cos(beta) * math.cos(gamma),
        )
    )
    return round(a * b * c * factor, 6)


def _split_cif_row(line: str) -> list[str]:
    return re.findall(r"'[^']*'|\"[^\"]*\"|\S+", line)


def _parse_cif(text: str) -> dict[str, Any]:
    scalar_fields: dict[str, str] = {}
    atom_sites: list[dict[str, Any]] = []
    lines = [line.strip() for line in text.splitlines()]
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line or line.startswith("#"):
            index += 1
            continue
        if line.startswith("_"):
            parts = _split_cif_row(line)
            if len(parts) >= 2:
                scalar_fields[parts[0]] = _clean_value(" ".join(parts[1:]))
            index += 1
            continue
        if line == "loop_":
            index += 1
            headers: list[str] = []
            while index < len(lines) and lines[index].startswith("_"):
                headers.append(lines[index])
                index += 1
            rows: list[list[str]] = []
            while index < len(lines):
                row_line = lines[index]
                if not row_line or row_line.startswith("#"):
                    index += 1
                    continue
                if row_line == "loop_" or row_line.startswith("_") or row_line.startswith("data_"):
                    break
                rows.append(_split_cif_row(row_line))
                index += 1
            if any(header.startswith("_atom_site_") for header in headers):
                for row in rows:
                    mapped = {
                        header: _clean_value(value)
                        for header, value in zip(headers, row, strict=False)
                    }
                    symbol = mapped.get("_atom_site_type_symbol")
                    if not symbol:
                        match = re.match(r"[A-Z][a-z]?", mapped.get("_atom_site_label", "") or "")
                        symbol = match.group(0) if match else ""
                    atom_sites.append(
                        {
                            "label": mapped.get("_atom_site_label", ""),
                            "type_symbol": symbol,
                            "fract_x": _to_float(mapped.get("_atom_site_fract_x", "0")),
                            "fract_y": _to_float(mapped.get("_atom_site_fract_y", "0")),
                            "fract_z": _to_float(mapped.get("_atom_site_fract_z", "0")),
                            "occupancy": _to_float(mapped.get("_atom_site_occupancy", "1"), 1.0),
                        }
                    )
            continue
        index += 1
    return {"scalars": scalar_fields, "atom_sites": atom_sites}


@materials_server.tool()
def inspect_cif(filepath: str, max_sites: int = 40) -> dict[str, Any]:
    """Inspect a CIF crystal structure file for cell, formula, and atom-site metadata.

    Agent story: Use this when a user asks whether a structure file is ready for
    materials analysis, simulation setup, density sanity checks, or collaborator
    review.

    Args:
        filepath: Path to a CIF file.
        max_sites: Maximum atom-site rows to include.

    Returns:
        Dictionary with unit-cell parameters, volume, formula fields, species
        counts, representative atom sites, and an approximate density if enough
        metadata is available.
    """
    try:
        safe_path = validate_read_path(filepath)
        max_sites = max(1, min(int(max_sites or 40), 200))
        parsed = _parse_cif(safe_path.read_text(encoding="utf-8"))
        scalars = parsed["scalars"]
        atom_sites = parsed["atom_sites"]
        cell = {
            "a": _to_float(scalars.get("_cell_length_a", "0")),
            "b": _to_float(scalars.get("_cell_length_b", "0")),
            "c": _to_float(scalars.get("_cell_length_c", "0")),
            "alpha": _to_float(scalars.get("_cell_angle_alpha", "90"), 90.0),
            "beta": _to_float(scalars.get("_cell_angle_beta", "90"), 90.0),
            "gamma": _to_float(scalars.get("_cell_angle_gamma", "90"), 90.0),
        }
        volume = _cell_volume(cell)
        species = Counter(
            str(site["type_symbol"])
            for site in atom_sites
            if site.get("type_symbol") and str(site["type_symbol"]) != "?"
        )
        occupied_counts: defaultdict[str, float] = defaultdict(float)
        for site in atom_sites:
            symbol = str(site.get("type_symbol") or "")
            if not symbol or symbol == "?":
                continue
            occupied_counts[symbol] += float(site.get("occupancy") or 0.0)
        molar_mass = sum(
            ATOMIC_WEIGHTS.get(symbol, 0.0) * count for symbol, count in occupied_counts.items()
        )
        density_g_cm3 = 0.0
        if volume > 0 and molar_mass > 0:
            density_g_cm3 = round(molar_mass / 6.02214076e23 / (volume * 1.0e-24), 6)
        return {
            "filepath": str(safe_path),
            "data_block": next((line for line in safe_path.read_text(encoding="utf-8").splitlines() if line.startswith("data_")), ""),
            "formula_sum": scalars.get("_chemical_formula_sum", ""),
            "formula_structural": scalars.get("_chemical_formula_structural", ""),
            "space_group": scalars.get("_symmetry_space_group_name_H-M", "")
            or scalars.get("_space_group_name_H-M_alt", ""),
            "cell": cell,
            "cell_volume_angstrom3": volume,
            "atom_site_count": len(atom_sites),
            "species_counts": dict(species),
            "occupancy_weighted_species_counts": dict(occupied_counts),
            "approx_density_g_cm3": density_g_cm3,
            "atom_sites": atom_sites[:max_sites],
            "atom_sites_truncated": len(atom_sites) > max_sites,
            "ok": True,
        }
    except FilePolicyError as exc:
        return exc.to_result()
    except Exception as exc:
        return {"error": str(exc)}
