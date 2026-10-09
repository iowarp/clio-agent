"""Build a labelled synthetic report using CLIO's actual dashboard catalog."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def build_report() -> dict[str, Any]:
    """Connect one small station dataset to occupancy, balance, location and evidence."""
    stations = [
        ("fern", "Fernhill", "Hill", 24, [21, 16, 4, 0, 0, 0], 41.885, -87.675),
        ("orchard", "Orchard", "Hill", 18, [18, 15, 8, 5, 3, 2], 41.89, -87.668),
        ("canal", "Canal Street", "Downtown", 24, [3, 8, 20, 24, 24, 24], 41.88, -87.637),
        ("market", "Market Square", "Downtown", 18, [2, 5, 12, 15, 17, 18], 41.875, -87.631),
        ("park", "Park", "Other areas", 20, [10, 10, 10, 10, 10, 10], 41.898, -87.645),
    ]
    times = ["at_6", "at_7", "at_8", "at_815", "at_830", "at_9"]
    rows = [
        dict(
            station_id=id_,
            name=name,
            category=category,
            capacity=capacity,
            latitude=lat,
            longitude=lon,
            net_bikes=values[-1] - values[0],
            **dict(zip(times, values, strict=True)),
        )
        for id_, name, category, capacity, values, lat, lon in stations
    ]
    occupancy = {
        "data": {"name": "source"},
        "transform": [
            {"fold": times, "as": ["time_key", "bikes"]},
            {
                "calculate": "datum.time_key==='at_815'?8.25:datum.time_key==='at_830'?8.5:toNumber(substring(datum.time_key,3))",
                "as": "hour",
            },
        ],
        "encoding": {
            "x": {
                "field": "hour",
                "type": "quantitative",
                "scale": {"domain": [6, 9]},
                "axis": {
                    "title": "Morning time",
                    "values": [6, 7, 8, 8.25, 9],
                    "labelExpr": "datum.value===8.25?'8:15':format(datum.value,'.0f')+':00'",
                },
            },
            "y": {
                "field": "bikes",
                "type": "quantitative",
                "title": "Bikes docked",
                "scale": {"domain": [0, 26]},
            },
            "color": {"field": "category", "type": "nominal", "legend": {"title": None}},
        },
        "layer": [
            {
                "mark": {"type": "line", "point": True},
                "encoding": {
                    "detail": {"field": "station_id"},
                    "tooltip": [{"field": "name"}, {"field": "hour"}, {"field": "bikes"}],
                },
            },
            {
                "transform": [{"filter": "datum.station_id==='fern' && datum.hour===8.25"}],
                "mark": {"type": "point", "filled": False, "size": 420, "strokeWidth": 2},
                "encoding": {"color": {"value": "#b91c1c"}},
            },
            {
                "transform": [{"filter": "datum.station_id==='fern' && datum.hour===8.25"}],
                "mark": {"type": "text", "align": "right", "dx": -8, "dy": -18},
                "encoding": {
                    "text": {"value": "Fernhill empty, 8:15 am"},
                    "color": {"value": "#b91c1c"},
                },
            },
        ],
    }
    components: list[dict[str, Any]] = [
        {
            "id": "root",
            "component": "Column",
            "children": [
                "scope",
                "metrics",
                "finding",
                "primary",
                "secondary",
                "limits",
                "sourceFrame",
            ],
        },
        {
            "id": "scope",
            "component": "Text",
            "text": "SYNTHETIC EXAMPLE · Five fictional stations · 6–9 am · Counts and locations illustrate the workflow.",
        },
        {"id": "metrics", "component": "Grid", "columns": 3, "children": ["empty", "loss", "gain"]},
        {
            "id": "empty",
            "component": "clio.metric.v1",
            "label": "Fernhill first empty",
            "value": "8:15 am",
            "detail": "At the first recorded zero count",
        },
        {
            "id": "loss",
            "component": "clio.metric.v1",
            "label": "Hill net change",
            "value": -37,
            "unit": "bikes",
            "detail": "6–9 am, two stations",
        },
        {
            "id": "gain",
            "component": "clio.metric.v1",
            "label": "Downtown net change",
            "value": "+37",
            "unit": "bikes",
            "detail": "Same interval, two stations",
        },
        {
            "id": "primary",
            "component": "Row",
            "align": "stretch",
            "children": ["trendFrame", "balanceFrame"],
        },
        {
            "id": "trendFrame",
            "component": "Frame",
            "weight": 2,
            "child": "occupancy",
        },
        {
            "id": "balanceFrame",
            "component": "Frame",
            "weight": 1,
            "child": "balance",
        },
        {
            "id": "secondary",
            "component": "Row",
            "align": "stretch",
            "children": ["mapFrame", "comparisonFrame"],
        },
        {
            "id": "mapFrame",
            "component": "Frame",
            "weight": 1.5,
            "child": "locations",
        },
        {
            "id": "comparisonFrame",
            "component": "Frame",
            "weight": 1.5,
            "title": "Compare the same stations",
            "description": "Select a station in the map, chart or table to highlight it across the report.",
            "child": "comparison",
        },
        {
            "id": "sourceFrame",
            "component": "Frame",
            "title": "Every source snapshot",
            "description": "Five fictional stations, all six recorded times. The editable data remains intact.",
            "child": "evidence",
        },
        {
            "id": "finding",
            "component": "Text",
            "text": "Hill occupancy falls as downtown occupancy rises. Fernhill reaches zero at 8:15 am; the matched totals show an imbalance, not proof of individual trip routes.",
        },
        {
            "id": "occupancy",
            "component": "clio.chart.v1",
            "title": "Bike availability, 6–9 am",
            "height": 290,
            "data": rows,
            "selectionField": "station_id",
            "spec": occupancy,
        },
        {
            "id": "limits",
            "component": "Text",
            "text": "The five stations conserve 54 bikes in every recorded interval. These synthetic snapshots do not measure missed trips or prove that an individual bike travelled between districts. The related views below share station identities; the source table retains every input count.",
        },
        {
            "id": "balance",
            "component": "clio.chart.v1",
            "title": "Net change by station, 6–9 am",
            "height": 290,
            "data": rows,
            "selectionField": "station_id",
            "spec": {
                "data": {"name": "source"},
                "mark": "bar",
                "encoding": {
                    "x": {
                        "field": "net_bikes",
                        "type": "quantitative",
                        "title": "Bikes gained / lost",
                        "scale": {"zero": True},
                    },
                    "y": {
                        "field": "name",
                        "type": "nominal",
                        "sort": {"field": "net_bikes"},
                        "title": None,
                    },
                    "color": {"field": "category", "type": "nominal", "legend": {"title": None}},
                    "tooltip": [{"field": "name"}, {"field": "net_bikes"}],
                },
            },
        },
        {
            "id": "locations",
            "component": "clio.map.v1",
            "title": "Illustrative station locations",
            "camera": {"path": "/camera"},
            "points": [
                {
                    "id": row["station_id"],
                    "label": row["name"],
                    "latitude": row["latitude"],
                    "longitude": row["longitude"],
                    "category": row["category"],
                }
                for row in rows
            ],
        },
        {
            "id": "comparison",
            "component": "clio.data-table.v1",
            "rows": rows,
            "selectionField": "station_id",
            "columns": [
                {"key": "name", "label": "Station"},
                {"key": "capacity", "label": "Docks"},
                {"key": "at_6", "label": "6 am"},
                {"key": "at_9", "label": "9 am"},
                {"key": "net_bikes", "label": "Net bikes"},
            ],
        },
        {"id": "evidence", "component": "Column", "children": ["method", "records"]},
        {
            "id": "method",
            "component": "Text",
            "text": "Net change = count at 9 am minus count at 6 am. Snapshot intervals are not uniform: 6, 7, 8, 8:15, 8:30 and 9 am. Station capacities are included. All inputs are fictional; the example demonstrates composition and interaction.",
        },
        {
            "id": "records",
            "component": "clio.data-table.v1",
            "rows": rows,
            "selectionField": "station_id",
            "columns": [
                {"key": "name", "label": "Station"},
                {"key": "category", "label": "Area"},
                {"key": "capacity", "label": "Capacity"},
                {"key": "at_6", "label": "6 am"},
                {"key": "at_7", "label": "7 am"},
                {"key": "at_8", "label": "8 am"},
                {"key": "at_815", "label": "8:15 am"},
                {"key": "at_830", "label": "8:30 am"},
                {"key": "at_9", "label": "9 am"},
                {"key": "net_bikes", "label": "Net bikes"},
                {"key": "station_id", "label": "Station ID"},
                {"key": "latitude", "label": "Latitude"},
                {"key": "longitude", "label": "Longitude"},
            ],
        },
    ]
    return {
        "format": "clio.dashboard.document.v1",
        "title": "Morning availability — Hill loses 37 bikes",
        "components": components,
        "data_model": {
            "camera": {"longitude": -87.655, "latitude": 41.886, "zoom": 12},
        },
    }


def main() -> None:
    """Write the agent-loadable source and the exact demonstration table."""
    root = Path(__file__).resolve().parents[2]
    reference = root / "src/clio_agent/gact/builtin_skills/create-dashboard/references"
    report = build_report()
    (reference / "bike-station-report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8"
    )
    rows = next(
        component["data"] for component in report["components"] if component["id"] == "occupancy"
    )
    with (reference / "bike-stations.csv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
