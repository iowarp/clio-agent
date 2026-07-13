"""EarthScope interactive multi-turn semantics — demo-confidence suite (#682/#687).

A live scientist drives the session conversationally, so this suite stresses how the
blueprint MANAGES CONTEXT across turns: reuse vs restart, anaphora ("the first city",
"the 2nd one", "that image"), context-switch-and-return, coordinates<->name, negation,
provenance-without-rerun, and underspecified follow-ups.

Each scenario is one session of several turns (the SUT's ``turns`` mode runs them on a
single session and returns per-turn sub-runs in ``run.extra['turn_runs']``). The
assertions are the *demo contract*: the robust, trace-observable signals —
  - a turn that does NOT change the place must NOT re-geocode (reuse), and a turn that
    DOES change the place must geocode the new one (switch);
  - a pure meta/provenance question must not re-stage data (answer from memory);
  - an honest no-coverage region mid-conversation must not fabricate a station;
  - the requested artifact (plot / overlay) is produced.
Incremental-reuse *efficiency* (5->7 == stage only 2 more) is reported from the trace
rather than hard-asserted here — it's the thing we're measuring.

Bar: gemma4/ALCF must pass; qwopus is best-effort. Run, e.g.::

    CLIO_RUN_LIVE=1 CLIO_GACT_FIXTURE_PORT=18996 \
      uv run pytest tests/test_real_cases/test_earthscope_interactive.py \
        -k e1_count --provider argonne_sophia --model google/gemma-4-31B-it \
        -o addopts="" -p no:cacheprovider -q
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

BLUEPRINT = "earthscope-gnss-region"
CASE_DIR = "benchmark/case02-earthscope-csv-seismic-geography"


# --- per-turn readers (turn_runs are Run.to_dict() dicts) ---------------------


def _names(tr: dict) -> list[str]:
    return [str(c.get("name")) for c in (tr.get("tool_calls") or [])]


def _geocoded(tr: dict) -> bool:
    return "geo_geocode" in _names(tr)


def _staged(tr: dict) -> list[str]:
    """Station ids whose time-series CSV was staged in THIS turn."""
    ids: list[str] = []
    for call in tr.get("tool_calls") or []:
        if call.get("name") != "ndp_stage_resource":
            continue
        out = call.get("output") if isinstance(call.get("output"), dict) else {}
        base = Path(str(out.get("local_path") or "")).name
        if base and not base.startswith("earthscope_converted") and base.endswith(".csv"):
            sid = base.split(".")[0]
            if sid and sid not in ids:
                ids.append(sid)
    return ids


def _pngs(tr: dict) -> list[str]:
    arts = (tr.get("extra") or {}).get("artifacts") or []
    return [p for p in arts if str(p).endswith(".png")]


def _plot_series_count(tr: dict) -> int:
    """How many station series the turn's plot drew (1 + overlay_paths)."""
    best = 0
    for call in tr.get("tool_calls") or []:
        if call.get("name") != "plot_plot_timeseries":
            continue
        args = call.get("args") or {}
        ov = args.get("overlay_paths") or []
        best = max(best, 1 + (len(ov) if isinstance(ov, list) else 0))
    return best


# --- scenario model -----------------------------------------------------------


@dataclass(frozen=True)
class Scene:
    label: str
    turns: tuple[str, ...]
    check: Callable[[list[dict], Any], None]


# --- the 10 scenarios + their demo-contract checks ----------------------------


def check_e1(trs: list[dict], run: Any) -> None:
    t1, t2, t3 = trs
    assert _geocoded(t1), "turn 1 should geocode Los Angeles"
    assert _pngs(t1), "turn 1 should plot"
    assert not _geocoded(t2), "count change (5->7) must reuse the city, not re-geocode"
    assert not _geocoded(t3), "count change (->3) must reuse the city, not re-geocode"


def check_e2(trs: list[dict], run: Any) -> None:
    t1, t2, t3 = trs
    assert _geocoded(t1), "turn 1 should geocode Palm Springs"
    assert not _geocoded(t2), "radius narrowing must reuse the city, not re-geocode"
    assert not _geocoded(t3), "radius widening must reuse the city, not re-geocode"


def check_e3(trs: list[dict], run: Any) -> None:
    t1, t2, t3 = trs
    assert _geocoded(t1), "turn 1 should geocode San Diego"
    assert _geocoded(t2), "switching to Seattle should geocode the new city"
    assert not _geocoded(t3), "'the San Diego ones' must reuse turn-1's region, not re-geocode"


def check_e4(trs: list[dict], run: Any) -> None:
    t1, t2 = trs
    assert _geocoded(t1), "turn 1 should geocode Palm Springs"
    assert not _geocoded(t2), "'the 2nd one' must reuse the ranked list, not re-geocode"
    assert _staged(t2), "turn 2 should stage the referenced station"
    assert _pngs(t2), "turn 2 should plot the referenced station"


def check_e5(trs: list[dict], run: Any) -> None:
    t1, t2, t3 = trs
    assert _geocoded(t1) and _pngs(t1), "turn 1 should geocode Reno + plot"
    assert not _geocoded(t2), "'redo that' must reuse, not re-geocode"
    assert _pngs(t2), "turn 2 should re-render the plot"
    assert not _geocoded(t3), "overlay request must reuse, not re-geocode"
    assert _plot_series_count(t3) >= 2, "turn 3 should overlay multiple stations on one figure"


def check_e6(trs: list[dict], run: Any) -> None:
    t1, t2, t3 = trs
    assert _geocoded(t1), "turn 1 geocodes San Diego"
    assert _geocoded(t2), "turn 2 switches to Reno -> geocode"
    assert not _geocoded(t3), "'back to San Diego' must reuse turn-1 region after the Reno detour"
    assert _pngs(t3), "turn 3 should plot San Diego"


def check_e7(trs: list[dict], run: Any) -> None:
    t1, t2, t3 = trs
    assert not _geocoded(t1), "explicit coordinates -> geocoding must be skipped"
    assert _geocoded(t2), "a place NAME (Chicago) -> geocode"
    assert not _staged(t2), "Chicago has no EarthScope GNSS coverage -> no station staged (honest)"
    assert not _geocoded(t3), "'back to the coordinates' must reuse, not re-geocode"


def check_e8(trs: list[dict], run: Any) -> None:
    t1, t2 = trs
    assert _geocoded(t1), "turn 1 geocodes Los Angeles"
    assert not _geocoded(t2), "drop/substitute must reuse the region, not re-geocode"
    assert _staged(t2), "turn 2 should stage a substitute station for the dropped one"


def check_e9(trs: list[dict], run: Any) -> None:
    t1, t2 = trs
    assert _geocoded(t1) and _pngs(t1), "turn 1 geocodes Santa Barbara + plots"
    assert not _geocoded(t2), "a provenance question must not re-run geocoding"
    assert not _staged(t2), "a provenance question must answer from memory, not re-stage data"


def check_e10(trs: list[dict], run: Any) -> None:
    t1, t2 = trs
    assert _geocoded(t1), "turn 1 geocodes Santa Barbara"
    assert not _geocoded(t2), "'do the usual' must reuse the resolved region"
    assert _pngs(t2) or _staged(t2), "'do the usual' should carry the pipeline forward (stage/plot)"


SCENES: tuple[Scene, ...] = (
    Scene(
        "e1_count_revision",
        (
            "Find the 5 EarthScope GNSS stations nearest to Los Angeles and plot their vertical displacement on one chart.",
            "Actually, make it 7 instead.",
            "Hmm, on second thought just do 3.",
        ),
        check_e1,
    ),
    Scene(
        "e2_radius_revision",
        (
            "Which EarthScope GNSS stations are near Palm Springs, California?",
            "Only the ones within 25 km, please.",
            "Okay, widen it back out to 75 km.",
        ),
        check_e2,
    ),
    Scene(
        "e3_first_city_anaphora",
        (
            "Find the 5 EarthScope GNSS stations nearest to San Diego.",
            "Oh cool — what about around Seattle?",
            "Okay, let's do the analysis on the San Diego ones.",
        ),
        check_e3,
    ),
    Scene(
        "e4_list_index_ref",
        (
            "List the 5 EarthScope GNSS stations nearest to Palm Springs, California.",
            "Stage and plot the 2nd one's displacement.",
        ),
        check_e4,
    ),
    Scene(
        "e5_artifact_ref",
        (
            "Find the EarthScope GNSS station nearest to Reno, Nevada and plot its east, north and up displacement.",
            "Can you redo that with just the up component?",
            "Now overlay the next two nearest stations on that same chart.",
        ),
        check_e5,
    ),
    Scene(
        "e6_city_hop_return",
        (
            "Find the EarthScope GNSS station nearest to San Diego and stage its data.",
            "What about near Reno, Nevada?",
            "Let's go back to San Diego and plot its displacement.",
        ),
        check_e6,
    ),
    Scene(
        "e7_coords_name_back",
        (
            "Find the nearest EarthScope GNSS station within 30 km of 35.8997, -120.4327 and stage it.",
            "Now do the same for Chicago, Illinois.",
            "Okay, go back to the coordinates and plot that station's displacement.",
        ),
        check_e7,
    ),
    Scene(
        "e8_drop_substitute",
        (
            "Find the 5 EarthScope GNSS stations nearest to Los Angeles and stage them.",
            "Drop MTA1 — it's too noisy — and use the next nearest one instead.",
        ),
        check_e8,
    ),
    Scene(
        "e9_provenance_query",
        (
            "Find the EarthScope GNSS station nearest to Santa Barbara, California and plot its displacement.",
            "Which station is that, and where did the data come from?",
        ),
        check_e9,
    ),
    Scene(
        "e10_underspecified",
        (
            "Is there EarthScope GNSS data near Santa Barbara, California?",
            "Great — do the usual.",
        ),
        check_e10,
    ),
)


@pytest.mark.real_case
@pytest.mark.live
@pytest.mark.parametrize("scene", SCENES, ids=[s.label for s in SCENES])
def test_earthscope_interactive(agent, gact_server, scene, tmp_path):
    run = agent.run(
        {
            "turns": list(scene.turns),
            "blueprint_id": BLUEPRINT,
            "case_dir": CASE_DIR,
            "run_label": scene.label,
            "workdir": str(tmp_path),
            "trace_path": str(gact_server.trace_dir / f"{scene.label}.run.jsonl"),
            "timeout_s": 0,
        }
    )

    assert run.error is None, run.error
    assert run.extra["blueprint_activated"], run.extra.get("active_agent_blueprint_id")
    turn_runs = run.extra.get("turn_runs") or []
    assert len(turn_runs) == len(scene.turns), (
        f"expected {len(scene.turns)} turn sub-runs, got {len(turn_runs)}"
    )
    scene.check(turn_runs, run)
