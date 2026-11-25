"""Search results carry where and when each record says its data is, by source field."""

from __future__ import annotations

import json

from clio_agent.gact.storage.ndp_presentations import ndp_presentation
from clio_agent.gact.storage.ndp_record_coverage import record_coverage

POLYGON = {
    "type": "Polygon",
    "coordinates": [[[-120.17, 38.94], [-119.92, 38.94], [-119.92, 39.25], [-120.17, 38.94]]],
}


def extras(**values: object) -> dict:
    return {"extras": [{"key": k, "value": v} for k, v in values.items()]}


def test_spatial_geojson_and_temporal_json_in_extras() -> None:
    record = extras(
        spatial=json.dumps(POLYGON),
        temporal=json.dumps({"startTime": "2010-08-11T00:00:00Z", "endTime": "2010-08-24"}),
    )
    assert record_coverage(record) == {
        "location": {
            "field": "spatial",
            "bbox": [-120.17, 38.94, -119.92, 39.25],
            "span_degrees": [0.25, 0.31],
        },
        "time": {"field": "temporal", "start": "2010-08-11T00:00:00Z", "end": "2010-08-24"},
    }


def test_each_organizations_own_fields_are_read() -> None:
    assert record_coverage(extras(dataBbox="-120,38,-119,39"))["location"]["bbox"] == [
        -120.0,
        38.0,
        -119.0,
        39.0,
    ]
    assert record_coverage(extras(start_time="2024-12-03", end_time="2024-12-12"))["time"] == {
        "field": "start_time/end_time",
        "start": "2024-12-03",
        "end": "2024-12-12",
    }
    assert record_coverage({"startDateTime": "2020", "endDateTime": "2021"})["time"]["field"] == (
        "startDateTime/endDateTime"
    )
    assert record_coverage(extras(issueDate="2023-05-01"))["time"] == {
        "field": "issueDate",
        "issued": "2023-05-01",
    }


def test_a_world_box_is_reported_with_its_span_and_nothing_is_invented() -> None:
    world = {"type": "Polygon", "coordinates": [[[-180, -90], [180, -90], [180, 90], [-180, -90]]]}
    assert record_coverage(extras(spatial=json.dumps(world)))["location"]["span_degrees"] == [
        360.0,
        180.0,
    ]
    assert record_coverage({"title": "Cruise metadata"}) == {"location": None, "time": None}
    assert record_coverage(extras(spatial="somewhere near the lake"))["location"] == {
        "field": "spatial",
        "unparsed": "somewhere near the lake",
    }


def test_search_presentation_shows_the_coverage_lines() -> None:
    row = {
        "datasets": [
            {
                "id": "d",
                "title": "Lake Tahoe Basin Lidar",
                "organization": "opentopography",
                "endpoint_id": "ndp",
                "resources": [],
                "coverage": {
                    "location": {"field": "spatial", "bbox": [-120.26, 38.7, -119.87, 39.33]},
                    "time": {"field": "temporal", "start": "2010-08-11", "end": "2010-08-24"},
                },
            },
            {
                "id": "e",
                "title": "Cruise",
                "endpoint_id": "ndp",
                "resources": [],
                "coverage": {"location": None, "time": None},
            },
        ],
        "total": 2,
    }
    text = json.dumps(ndp_presentation({"query": "tahoe"}, row, None))
    assert "Location (spatial): -120.26, 38.7, -119.87, 39.33" in text
    assert "Time (temporal): 2010-08-11 to 2010-08-24" in text
    assert "Location: not stated in the record" in text
