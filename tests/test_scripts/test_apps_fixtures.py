"""The apps test-ground fixtures stay small, well formed and reproducible (apps ledger, grounds)."""

from __future__ import annotations

import importlib.util
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "apps"
_spec = importlib.util.spec_from_file_location("wave_series", FIXTURES / "wave_series.py")
assert _spec and _spec.loader
wave: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wave)


def test_committed_series_matches_the_generator() -> None:
    committed = FIXTURES / "wave"
    names = sorted(p.name for p in committed.iterdir())
    assert names == ["wave.pvd"] + [f"wave_{s:04d}.vti" for s in range(6)]
    assert (committed / "wave.pvd").read_text() == wave.collection_xml(6)
    for step in range(6):
        assert (committed / f"wave_{step:04d}.vti").read_text() == wave.step_xml(step)
    assert sum(p.stat().st_size for p in committed.iterdir()) < 200_000


def test_step_is_vtk_image_data_with_both_fields() -> None:
    root = ET.fromstring(wave.step_xml(3))
    arrays = root.findall(".//PointData/DataArray")
    assert [a.get("Name") for a in arrays] == ["pressure", "temperature"]
    for array in arrays:
        assert len((array.text or "").split()) == wave.SIZE**3


def test_grow_writes_one_step_at_a_time(tmp_path: Path) -> None:
    wave.grow(tmp_path, steps=3, interval=0)
    collection = ET.parse(tmp_path / "wave.pvd").getroot()
    times = [float(d.get("timestep")) for d in collection.iter("DataSet")]
    assert times == [0.0, 0.1, 0.2]
    assert not list(tmp_path.glob(".*.partial"))


def test_netlist_names_the_output_node() -> None:
    text = (FIXTURES / "rc_lowpass.cir").read_text()
    assert " out " in text and ".tran" in text and text.rstrip().endswith(".end")
