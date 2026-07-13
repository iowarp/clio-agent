"""EarthScope generalization — one blueprint, many query shapes (#682).

The original EarthScope case (``test_earthscope_case.py``) proves the blueprint
on ONE verbose query across models. This file proves the SAME blueprint
generalizes across distinct, SHORT, generic query shapes on one driver model
(gemma4/ALCF first), judged on the *structured* trace — the tool path actually
taken — not on prose:

  (b) #684  city -> coordinates via the geo_geocode tool (grounded, not a prior)
  (c) #685  explicit coordinates supplied -> geocoding is SKIPPED
  (d) #686  an explicit radius in the prompt is honored (and never widened)
  (a) #683  variable-N multi-station overlay        [added with its blueprint work]
  (e) #687  progressive multi-turn in one session   [added with its harness work]

Run one cell on gemma4/ALCF (use a DISTINCT fixture port so it never collides
with a concurrent grind on :17960)::

    CLIO_RUN_LIVE=1 CLIO_GACT_FIXTURE_PORT=18995 \
      CLIO_AGENTTEST_CELLS="argonne_sophia:google/gemma-4-31B-it" \
      CLIO_AGENTTEST_CONTEXT_LENGTH=262144 CLIO_AGENTTEST_NO_PROGRESS_S=1800 \
      uv run pytest tests/test_real_cases/test_earthscope_generalization.py \
        -k case_d_radius --provider argonne_sophia --model google/gemma-4-31B-it \
        -o addopts="" -p no:cacheprovider -q
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from agent_test import matcher

BLUEPRINT = "earthscope-gnss-region"
CASE_DIR = "benchmark/case02-earthscope-csv-seismic-geography"


@dataclass(frozen=True)
class GenCell:
    """One generalization cell: a short, generic prompt plus the structured
    expectations unique to the query shape it probes."""

    label: str
    prompt: str
    expect: str = "positive"  # "positive" (stage+plot) | "negative" (no coverage)
    geocode: str = "either"  # "called" | "skipped" | "either"
    radius_km: float = 0.0  # >0 => the filter must run at exactly this radius
    min_stations: int = 1  # distinct staged station CSVs required (a => >1)


# Distinct from the SD/Seattle/Chicago grind; all western-US covered regions.
GEN_CELLS: tuple[GenCell, ...] = (
    GenCell(
        label="case_b_geocode",
        prompt=(
            "Find the EarthScope GNSS station closest to Palm Springs, California. "
            "Stage its time-series CSV and plot its east/north/up displacement."
        ),
        geocode="called",
    ),
    GenCell(
        label="case_c_coords",
        prompt=(
            "I already have the coordinates 35.8997, -120.4327. Find the nearest "
            "EarthScope GNSS station within 30 km, stage its CSV, and plot its displacement."
        ),
        geocode="skipped",
        radius_km=30.0,
    ),
    GenCell(
        label="case_d_radius",
        prompt=(
            "List the EarthScope GNSS stations within 50 km of Reno, Nevada, then "
            "stage and plot the closest one's displacement."
        ),
        geocode="called",
        radius_km=50.0,
    ),
)


# --- shared structured readers ------------------------------------------------


def _tool_result(run, name):
    for call in run.tool_calls:
        if call.name == name and isinstance(call.output, dict):
            return call.output
    return None


def _staged_station_ids(run) -> list[str]:
    """Every distinct station id whose time-series CSV was profiled/plotted
    (``P475.CI.LY_.20.csv`` -> ``P475``); ignores the metadata catalog CSV."""
    ids: list[str] = []
    for call in run.tool_calls:
        if call.name in ("plot_plot_timeseries", "pandas_profile_csv"):
            base = Path(str((call.args or {}).get("data_path", ""))).name
            if base and not base.startswith("earthscope_converted"):
                sid = base.split(".")[0]
                if sid and sid not in ids:
                    ids.append(sid)
    return ids


def _geospatial_state(run) -> dict:
    gs = (run.extra.get("workflow_state") or {}).get("geospatial")
    return gs if isinstance(gs, dict) else {}


def _filter_facts(run) -> tuple[float, dict[str, float]]:
    """Return (radius_km, {station_id: distance_km}) from the geo_filter result.

    Robust to truncation: a large filter result (many points) comes back as a
    ``{"preview": "<json text>", "truncated": true}`` envelope rather than a parsed
    dict, so read the numbers out of the serialized text. Each point serializes as
    ``... "distance_km": <n>, "id": "<sid>" ...`` and the result carries
    ``"radius_km": <n>`` — both survive truncation for the nearest stations."""
    import json as _json
    import re

    # Aggregate across EVERY geo_filter call: a multi-station request may backtrack
    # to the catalog and re-filter (2+ calls), and each large result is truncated to
    # a {"preview": "<raw json text>", "truncated": true} envelope (use the preview
    # text directly — json.dumps would escape its quotes and break the regex).
    radius = 0.0
    dists: dict[str, float] = {}
    for call in run.tool_calls:
        if call.name != "geo_filter_points_by_radius" or not isinstance(call.output, dict):
            continue
        fr = call.output
        blob = fr["preview"] if isinstance(fr.get("preview"), str) else _json.dumps(fr, default=str)
        m = re.search(r'"radius_km":\s*([0-9.]+)', blob)
        if m:
            radius = max(radius, float(m.group(1)))
        for dist, sid in re.findall(r'"distance_km":\s*([0-9.]+),\s*"id":\s*"([^"]+)"', blob):
            dists.setdefault(sid, float(dist))
    return radius, dists


def _resolver_staged_station_ids(run) -> list[str]:
    """Distinct station ids whose time-series CSV was actually STAGED via
    ndp_stage_resource (``.../MTA1.CI.LY_.30.csv`` -> ``MTA1``). Used for the
    multi-station case, where the plot may read a single MERGED CSV so the
    plot-path station id no longer reflects how many stations were gathered."""
    ids: list[str] = []
    for call in run.tool_calls:
        if call.name != "ndp_stage_resource":
            continue
        out = call.output if isinstance(call.output, dict) else {}
        base = Path(str(out.get("local_path") or "")).name
        if base and not base.startswith("earthscope_converted") and base.endswith(".csv"):
            sid = base.split(".")[0]
            if sid and sid not in ids:
                ids.append(sid)
    return ids


# --- matchers (frozen structured checks) --------------------------------------


@matcher
def ran_acquisition_to_plot_pipeline(run):
    n = run.tool_names
    return (
        "geo_filter_points_by_radius" in n
        and "ndp_stage_resource" in n
        and "pandas_profile_csv" in n
        and any("plot" in x for x in n)
    )


@matcher
def staged_station_on_region(run):
    radius, distances = _filter_facts(run)
    ids = _staged_station_ids(run)
    if not ids or radius <= 0:
        return False
    return all(sid in distances and distances[sid] <= radius for sid in ids)


@matcher
def produced_nonempty_png(run):
    return any(
        p.endswith(".png") and Path(p).is_file() and Path(p).stat().st_size > 1024
        for p in run.extra.get("artifacts", [])
    )


@matcher
def geocode_was_called(run):
    return "geo_geocode" in run.tool_names


@matcher
def geocode_grounded_region(run):
    """The region center came from the tool, not a model prior."""
    return str(_geospatial_state(run).get("provenance") or "") == "osm_nominatim"


@matcher
def radius_honored(run, expected_km: float):
    """The filter ran at the requested radius and the resolved region recorded it
    (no widening). Checks both the tool result and the typed geospatial state."""
    radius, _ = _filter_facts(run)
    tool_ok = abs(radius - expected_km) < 1.0
    gs = _geospatial_state(run)
    state_ok = gs.get("radius_km") is None or abs(float(gs["radius_km"]) - expected_km) < 1.0
    return tool_ok and state_ok


# (e) #687 — progressive exploration across turns of ONE session. Santa Barbara
# is NDP-covered (RCA2 ~9 km); the nearest station (EOCG) is NOT, so the resolver
# must iterate down the ranked list to a station that actually has a CSV.
PROGRESSIVE_TURNS = (
    "Does EarthScope have GNSS data for stations near Santa Barbara, California?",
    "Nice — take the closest station that has data and show me what its dataset contains.",
    "Now plot its east/north/up displacement.",
)


def _turn_tool_names(tr: dict) -> list[str]:
    return [str(c.get("name")) for c in (tr.get("tool_calls") or [])]


def _turn_pngs(tr: dict) -> list[str]:
    arts = (tr.get("extra") or {}).get("artifacts") or []
    return [p for p in arts if str(p).endswith(".png")]


@pytest.mark.real_case
@pytest.mark.live
def test_earthscope_progressive_session(agent, gact_server, tmp_path):
    """One session, three turns: discover -> inspect -> plot. The agent must do
    only what each turn asks (turn 1 discovers but does NOT plot) and reuse the
    session's region/stations on later turns rather than restarting."""
    run = agent.run(
        {
            "turns": list(PROGRESSIVE_TURNS),
            "blueprint_id": BLUEPRINT,
            "case_dir": CASE_DIR,
            "run_label": "progressive",
            "workdir": str(tmp_path),
            "trace_path": str(gact_server.trace_dir / "progressive.run.jsonl"),
            "timeout_s": 0,
        }
    )

    assert run.error is None, run.error
    assert run.extra["blueprint_activated"], run.extra.get("active_agent_blueprint_id")
    assert run.extra.get("session_id"), "no session id"

    turn_runs = run.extra.get("turn_runs") or []
    assert len(turn_runs) == 3, f"expected 3 turn sub-runs, got {len(turn_runs)}"
    t1, _t2, _t3 = turn_runs

    # Turn 1 is a discovery question: NO plot/PNG should be produced yet.
    assert not _turn_pngs(t1), f"turn 1 plotted but should only discover: {_turn_pngs(t1)}"
    assert not any("plot" in n for n in _turn_tool_names(t1)), _turn_tool_names(t1)

    # By the end of the session a real PNG exists (turn 3 plotted on-region data).
    assert produced_nonempty_png(run), run.extra.get("artifacts")
    assert staged_station_on_region(run), (
        f"final staged station off-region/unverifiable; ids={_staged_station_ids(run)}"
    )


# (a) #683 — variable-N multi-station overlay. Los Angeles is a dense NDP-covered
# cluster (MTA1 ~0.8 km + many CI-network neighbors), so several stations within a
# small radius actually have time-series CSVs to overlay.
@pytest.mark.real_case
@pytest.mark.live
@pytest.mark.parametrize("n", [3, 5], ids=["n3", "n5"])
def test_earthscope_multistation(agent, gact_server, n, tmp_path):
    """The agent stages N nearby stations and overlays them on one figure — N is
    taken from the prompt, not fixed. Counts stations from the staging tool, since
    the plot may read a single merged CSV."""
    prompt = (
        f"Plot the vertical (up) displacement for the {n} EarthScope GNSS stations "
        f"nearest to Los Angeles, all on one chart so I can compare how they move."
    )
    run = agent.run(
        {
            "task": prompt,
            "blueprint_id": BLUEPRINT,
            "case_dir": CASE_DIR,
            "run_label": f"multistation_n{n}",
            "workdir": str(tmp_path),
            "trace_path": str(gact_server.trace_dir / f"multistation_n{n}.run.jsonl"),
            "timeout_s": 0,
        }
    )

    assert run.error is None, run.error
    assert run.extra["blueprint_activated"], run.extra.get("active_agent_blueprint_id")

    staged = _resolver_staged_station_ids(run)
    radius, distances = _filter_facts(run)
    assert radius > 0, "no filter radius"

    # Coverage-aware target: NDP only has time-series for a SUBSET of stations, so
    # "the N nearest" may include stations with no CSV (e.g. USC2 near LA). The agent
    # should overlay every COVERED station it found among the nearest, up to N — pass
    # when it staged all that were available (staged == min(N, covered_found)), not
    # only when it hit exactly N. (`covered_found` = within-radius stations the agent
    # searched whose NDP lookup returned datasets — its own evidence, no extra calls.)
    covered_found = {
        str((c.args or {}).get("dataset_title") or "")
        for c in run.tool_calls
        if c.name == "ndp_search_datasets"
        and isinstance(c.output, dict)
        and isinstance(c.output.get("count"), (int, float))
        and c.output["count"] >= 1
        and str((c.args or {}).get("dataset_title") or "") in distances
    }
    target = min(n, len(covered_found)) if covered_found else n
    assert len(staged) >= target, (
        f"staged {staged} ({len(staged)}) but {len(covered_found)} covered stations were "
        f"found among the nearest — expected >= {target} (min of requested {n} and available)"
    )
    assert len(staged) >= 2, f"multi-station request overlaid too few: {staged}"

    # Each staged station must be within the requested region radius.
    for sid in staged:
        assert sid in distances and distances[sid] <= radius, (
            f"staged station {sid} off-region; filter radius={radius}"
        )

    assert produced_nonempty_png(run), run.extra.get("artifacts")


@pytest.mark.real_case
@pytest.mark.live
@pytest.mark.parametrize("cell", GEN_CELLS, ids=[c.label for c in GEN_CELLS])
def test_earthscope_generalization(agent, gact_server, cell, tmp_path):
    run = agent.run(
        {
            "task": cell.prompt,
            "blueprint_id": BLUEPRINT,
            "case_dir": CASE_DIR,
            "run_label": cell.label,
            "workdir": str(tmp_path),
            "trace_path": str(gact_server.trace_dir / f"{cell.label}.run.jsonl"),
            "timeout_s": 0,
        }
    )

    assert run.error is None, run.error
    assert run.extra["blueprint_activated"], run.extra.get("active_agent_blueprint_id")

    # Query-shape-specific tool-path checks (the point of this file).
    if cell.geocode == "called":
        assert geocode_was_called(run), f"geo_geocode not called: {run.tool_names}"
        assert geocode_grounded_region(run), _geospatial_state(run)
    elif cell.geocode == "skipped":
        assert not geocode_was_called(run), (
            f"geo_geocode called even though coordinates were supplied: {run.tool_names}"
        )

    if cell.radius_km:
        assert radius_honored(run, cell.radius_km), (
            f"radius not honored (expected {cell.radius_km} km); "
            f"filter={_tool_result(run, 'geo_filter_points_by_radius')}, "
            f"geospatial={_geospatial_state(run)}"
        )

    if cell.expect == "negative":
        assert not _staged_station_ids(run), _staged_station_ids(run)
        assert not produced_nonempty_png(run), run.extra.get("artifacts")
        return

    # Positive: the full real pipeline ran and plotted on-region station(s).
    assert run.routed_to("data"), run.steps
    assert run.routed_to("synthesis"), run.steps
    assert ran_acquisition_to_plot_pipeline(run), run.tool_names
    assert staged_station_on_region(run), (
        f"staged station off-region/unverifiable; ids={_staged_station_ids(run)}, "
        f"filter={_tool_result(run, 'geo_filter_points_by_radius')}"
    )
    assert produced_nonempty_png(run), run.extra.get("artifacts")
    assert len(_staged_station_ids(run)) >= cell.min_stations, (
        f"expected >= {cell.min_stations} staged stations, got {_staged_station_ids(run)}"
    )

    pngs = [p for p in run.extra.get("artifacts", []) if p.endswith(".png")]
    for p in pngs:
        assert Path(p).resolve().is_relative_to(tmp_path.resolve()), (
            f"PNG {p!r} written outside the isolated workdir {tmp_path}"
        )
