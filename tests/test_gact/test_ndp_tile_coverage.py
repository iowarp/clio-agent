"""Tile selection by region reads footprints from the data and never drops a tile."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest

from clio_agent.gact.storage import ndp_tile_coverage as coverage
from clio_agent.gact.storage.ndp_catalog import (
    NDPCatalog,
    NDPDataset,
    NDPEndpoint,
    NDPResource,
)
from clio_agent.gact.storage.ndp_tile_coverage import (
    parse_vrt,
    tile_coverage_presentation,
    tiles_covering,
)
from clio_agent.gact.tool_result_presentation import ToolPresentation

BASE = "https://topo.example/raster/LAKE/"
# A 2 x 2 mosaic of 1000 m tiles in UTM 10N with its origin at (750000, 4320000).
VRT = f"""<VRTDataset rasterXSize="2000" rasterYSize="2000">
  <SRS>EPSG:26910</SRS>
  <GeoTransform>750000, 1, 0, 4320000, 0, -1</GeoTransform>
  <VRTRasterBand dataType="Float32" band="1">
    <SimpleSource><SourceFilename relativeToVRT="1">LAKE_be/nw.tif</SourceFilename>
      <DstRect xOff="0" yOff="0" xSize="1000" ySize="1000"/></SimpleSource>
    <SimpleSource><SourceFilename relativeToVRT="1">LAKE_be/ne.tif</SourceFilename>
      <DstRect xOff="1000" yOff="0" xSize="1000" ySize="1000"/></SimpleSource>
    <ComplexSource><SourceFilename relativeToVRT="0">/vsicurl/{BASE}LAKE_be/sw.tif</SourceFilename>
      <DstRect xOff="0" yOff="1000" xSize="1000" ySize="1000"/></ComplexSource>
    <SimpleSource><SourceFilename relativeToVRT="0">C:/producer/se.tif</SourceFilename>
      <DstRect xOff="1000" yOff="1000" xSize="1000" ySize="1000"/></SimpleSource>
  </VRTRasterBand>
</VRTDataset>"""


def resource(name: str, fmt: str = "TIFF", url: str = "") -> NDPResource:
    return NDPResource(
        id=f"id-{name}", path=f"1-{name}", name=name, url=url or BASE + name, format=fmt
    )


def dataset(*resources: NDPResource, authenticated: bool = False) -> NDPDataset:
    endpoint = NDPEndpoint(
        id="ndp", name="NDP", url="https://topo.example", authenticated=authenticated
    )
    return NDPDataset(
        id="lake",
        name="lake",
        title="Lake Basin Lidar with a deliberately long title that wraps in narrow views",
        organization="opentopography",
        endpoint=endpoint,
        resources=list(resources),
    )


def test_vrt_footprints_resolve_relative_vsicurl_and_skip_producer_paths() -> None:
    crs, sources = parse_vrt(VRT, BASE + "LAKE_be.vrt")
    assert crs == "EPSG:26910"
    assert dict(sources) == {
        BASE + "LAKE_be/nw.tif": (750000.0, 4319000.0, 751000.0, 4320000.0),
        BASE + "LAKE_be/ne.tif": (751000.0, 4319000.0, 752000.0, 4320000.0),
        BASE + "LAKE_be/sw.tif": (750000.0, 4318000.0, 751000.0, 4319000.0),
    }


def test_rotated_mosaic_is_refused_not_guessed() -> None:
    with pytest.raises(ValueError, match="rotated"):
        parse_vrt(VRT.replace("750000, 1, 0,", "750000, 1, 0.5,"), BASE + "x.vrt")


def _tiles(monkeypatch: pytest.MonkeyPatch, *, header: Any = None) -> NDPDataset:
    monkeypatch.setattr(coverage, "_fetch_vrt", lambda url: VRT)
    monkeypatch.setattr(
        coverage, "_header", header or (lambda url: (_ for _ in ()).throw(OSError("HTTP 403")))
    )
    return dataset(
        resource("LAKE_be.vrt", ""),
        resource("LAKE_be/nw.tif"),
        resource("LAKE_be/ne.tif", "GeoTIFF"),
        resource("LAKE_be/sw.tif", ""),
        resource("LAKE_be/se.tif"),
        resource("footprint.json", "GeoJSON"),
    )


def test_region_selects_mosaic_tiles_and_reports_unreadable_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _tiles(monkeypatch)
    # nw tile spans about -120.1138..-120.1019 lon, 38.9840..38.9933 lat.
    result = tiles_covering(item, (-120.110, 38.988, -120.108, 38.990))
    assert [c["id"] for c in result["covering"]] == ["id-LAKE_be/nw.tif"]
    assert result["tiles"] == 4 and result["outside"] == 2
    assert result["undetermined"] == [
        {
            "id": "id-LAKE_be/se.tif",
            "name": "LAKE_be/se.tif",
            "reason": "header unreadable: HTTP 403",
        }
    ]
    assert result["other_resources"] == 2
    assert result["methods"] == ["mosaic LAKE_be.vrt (3 sources)", "1 raster headers"]


def test_headers_fill_tiles_the_mosaic_does_not_name(monkeypatch: pytest.MonkeyPatch) -> None:
    item = _tiles(
        monkeypatch, header=lambda url: ("EPSG:26910", (751000, 4318000, 752000, 4319000))
    )
    result = tiles_covering(item, (-121, 38, -119, 40))
    assert len(result["covering"]) == 4 and not result["undetermined"]


def test_header_budget_pages_instead_of_dropping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coverage, "MAX_HEADER_READS", 2)
    monkeypatch.setattr(coverage, "_header", lambda url: ("EPSG:4326", (-1, -1, 1, 1)))
    item = dataset(*(resource(f"t{n}.tif") for n in range(5)))
    first = tiles_covering(item, (-0.5, -0.5, 0.5, 0.5))
    assert len(first["covering"]) == 2 and first["next_header_offset"] == 2
    assert {u["reason"] for u in first["undetermined"]} == {
        "header not read in this call; ask for the next header page"
    }
    last = tiles_covering(item, (-0.5, -0.5, 0.5, 0.5), header_offset=4)
    assert len(last["covering"]) == 1 and last["next_header_offset"] is None


def test_signed_in_endpoint_tiles_are_not_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(url: str) -> Any:
        raise AssertionError("a protected tile must not be fetched")

    monkeypatch.setattr(coverage, "_fetch_vrt", forbidden)
    monkeypatch.setattr(coverage, "_header", forbidden)
    item = dataset(resource("a.vrt", "VRT"), resource("a.tif"), authenticated=True)
    result = tiles_covering(item, (-1, -1, 1, 1))
    assert result["undetermined"][0]["reason"].startswith("served behind a signed-in Endpoint")
    assert "signed-in Endpoint" in result["notes"][0]


def test_unreadable_mosaic_is_a_note_and_arcgrid_stays_undetermined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coverage, "_fetch_vrt", lambda url: "<html>not found</html>")
    item = dataset(resource("dem.vrt", ""), resource("dem/w001001.adf", ""))
    result = tiles_covering(item, (-1, -1, 1, 1))
    assert result["notes"] == ["dem.vrt: mosaic could not be read (not a VRTDataset)"]
    assert result["undetermined"][0]["reason"] == "ArcGrid file outside any readable mosaic"


def test_arcgrid_glob_source_covers_the_coverage_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    glob = VRT.replace("LAKE_be/nw.tif", "LAKE_be/*.adf")
    monkeypatch.setattr(coverage, "_fetch_vrt", lambda url: glob)
    item = dataset(resource("LAKE_be.vrt", ""), resource("LAKE_be/w001001.adf", ""))
    result = tiles_covering(item, (-121, 38, -119, 40))
    assert [c["id"] for c in result["covering"]] == ["id-LAKE_be/w001001.adf"]


def test_invalid_region_is_refused() -> None:
    with pytest.raises(ValueError, match="west < east"):
        tiles_covering(dataset(), (10, 0, 5, 1))


def test_observed_requires_an_earlier_search() -> None:
    catalog = NDPCatalog()
    with pytest.raises(ValueError, match="connected_data_search_ndp first"):
        catalog.observed("lake")
    item = dataset(resource("a.tif"))
    catalog._results[("ndp", "lake")] = item
    assert catalog.observed("lake") is item
    assert catalog.observed("lake", "ndp") is item
    catalog._results[("ndp-test", "lake")] = item
    with pytest.raises(ValueError, match=r"several catalogs \(ndp, ndp-test\)"):
        catalog.observed("lake")


def _view(row: dict[str, Any], args: dict[str, Any] | None = None) -> tuple[dict[str, Any], str]:
    before = deepcopy(row)
    view = tile_coverage_presentation(args or {}, json.dumps(row), None)
    assert row == before
    ToolPresentation.model_validate(view)
    return view, json.dumps(view, ensure_ascii=False)


def _row(**changes: Any) -> dict[str, Any]:
    return {
        "dataset": {
            "id": "lake",
            "title": "Lake Tahoe Basin Lidar",
            "organization": "opentopography",
            "endpoint_id": "ndp",
        },
        "region": {"west": -120.13, "south": 38.94, "east": -120.11, "north": 38.96},
        "tiles": 171,
        "covering": [
            {
                "id": "r1",
                "name": "TAHOE - DTMhe_38120H11.img",
                "path": "3-he_38120H11.img",
                "bounds": [-120.14, 38.93, -120.1, 38.97],
            }
        ],
        "outside": 170,
        "undetermined": [],
        "other_resources": 3,
        "methods": ["mosaic TAHOE_be.vrt (58 sources)"],
        "notes": [],
        "next_header_offset": None,
        **changes,
    }


def test_presentation_success_names_tiles_counts_and_method() -> None:
    view, text = _view(_row())
    assert view["summary"] == "1 of 171 tiles cover the region"
    assert "Lake Tahoe Basin Lidar" in text and "-120.13, 38.94, -120.11, 38.96" in text
    assert "TAHOE - DTMhe_38120H11.img" in text and "r1" in text
    assert "mosaic TAHOE_be.vrt (58 sources)" in text
    assert "3 non-tile resources" in text


def test_presentation_empty_partial_and_failed_states() -> None:
    empty, text = _view(_row(covering=[], outside=171))
    assert empty["summary"] == "0 of 171 tiles cover the region"
    assert "No tile of this dataset covers the region." in text
    undetermined = [{"id": "u", "name": "se.tif", "reason": "header unreadable: HTTP 403"}]
    partial, text = _view(
        _row(
            undetermined=undetermined,
            next_header_offset=200,
            notes=["a.vrt: mosaic could not be read (HTTP 404)"],
        )
    )
    assert "header unreadable: HTTP 403" in text and "header_offset=200" in text
    assert "HTTP 404" in text
    counts = next(b for b in partial["blocks"] if b["id"] == "counts")
    assert counts["severity"] == "warning"
    failed, text = _view({"message": "Search for this NDP dataset first"})
    assert failed["summary"] == "Tile coverage unavailable" and "Search for this NDP" in text


def test_presentation_large_result_keeps_every_tile_identity() -> None:
    covering = [
        {
            "id": f"r{n}",
            "name": f"USGS_1M_10_x{n:03d}y430_with_a_long_project_name.tif",
            "path": f"{n}-t.tif",
            "bounds": [-120.0, 38.0, -119.9, 38.1],
        }
        for n in range(1000)
    ]
    view, text = _view(_row(covering=covering, tiles=1048, outside=48))
    assert view["summary"] == "1000 of 1048 tiles cover the region"
    assert "r999" in text and "USGS_1M_10_x999y430" in text


def test_search_warns_when_a_dataset_has_more_resources_than_can_be_selected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 3DEP project with 1,048 tiles must not lose 48 of them silently."""
    monkeypatch.delenv("CLIO_STORAGE_NDP_ENDPOINTS", raising=False)
    monkeypatch.delenv("NDP_API_URL", raising=False)
    catalog = NDPCatalog()
    record = {
        "id": "dem",
        "name": "dem",
        "title": "3DEP 1-m DEM Dataset: CA_SierraNevada_B22",
        "notes": "",
        "organization": {"name": "usgs-3dep"},
        "resources": [
            {"id": f"t{n}", "url": f"https://tiles.example/USGS_1M_{n}.tif", "format": "GeoTIFF"}
            for n in range(1048)
        ],
    }
    monkeypatch.setattr(catalog, "access", lambda endpoint, token: (None, ""))
    monkeypatch.setattr(catalog._public_catalog, "search", lambda *args: ([record], 1))
    result = catalog.search("sierra")
    assert result["warnings"] == [
        "3DEP 1-m DEM Dataset: CA_SierraNevada_B22: CLIO keeps the first 1000 of 1048 "
        "resources; the rest cannot be selected here."
    ]
    # A search hit lists a few resources, counts and tallies the rest, and pages them.
    row = result["datasets"][0]
    assert len(row["resources"]) == 20 and row["resource_count"] == 1000
    assert row["resources_listed"] == "1-20 of 1000" and row["next_resource_offset"] == 20
    assert row["resource_formats"] == {"GeoTIFF": 1000}
    page = catalog.resource_page("dem", resource_offset=900)
    listed = page["datasets"][0]
    assert [item["id"] for item in listed["resources"]][:1] == ["t900"]
    assert len(listed["resources"]) == 100 and "next_resource_offset" not in listed
    assert listed["resources_listed"] == "901-1000 of 1000"
    with pytest.raises(ValueError, match="has 1000 resources"):
        catalog.resource_page("dem", resource_offset=1000)


def test_search_hit_with_few_resources_lists_them_all() -> None:
    item = dataset(resource("a.tif"), resource("b.tif"))
    row = NDPCatalog.visible(item)
    assert row["resource_count"] == 2 and len(row["resources"]) == 2
    assert "resources_listed" not in row and "next_resource_offset" not in row


def test_a_blank_record_format_reports_the_file_suffix() -> None:
    """NDP leaves format blank on tiled lidar rasters; the file name still says .img."""
    blank = [
        NDPResource(id=f"t{n}", path=f"{n}-x.img", name=f"TILE - DTMhe_{n}.img", url="", format="")
        for n in range(30)
    ]
    item = dataset(*blank, resource("dem.vrt", fmt=""), resource("readme", fmt=""))
    row = NDPCatalog.visible(item)
    assert row["resources"][0]["name_suffix"] == ".img" and row["resources"][0]["format"] == ""
    assert row["resource_formats"] == {
        "unspecified (.img from the file name)": 30,
        "unspecified (.vrt from the file name)": 1,
        "unspecified": 1,
    }
