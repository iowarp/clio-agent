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

A2UI live gate (S7, docs/design/a2ui-compat-campaign-2026-09.md /
iowarp/clio-agent-marketplace#69 deliverable 5): three scenes below drive the
``earthscope-single-agent`` marketplace pack (a DIFFERENT, single-expert
blueprint from ``earthscope-gnss-region`` above — the one that ships the
``earthscope-stations`` A2UI catalog) through a real GNSS-station-selection
round trip on "Show me the GNSS stations around Los Angeles": idle (the turn
ends with the surface ready, the harness posts the selection, the NEXT turn's
tool calls stage exactly those two stations), queued (the same post while a
follow-up turn is still running — one record, delivered by steer, consumed
once at the drain), and waiting-user (the agent asks with ``surface_id``
bound — the post resumes that exact question). Assertions read the live
surface's own data model / ``a2ui_action`` records and the tool-call parts
via ``ClioAgent._extract_messages`` — never prose. Per the live-test rule
(subscription providers only; local qwopus/LM Studio kills the box on a
multi-session grind) and the headless pre-allow doctrine (the ``gact_server``
fixture already ``PUT``s a wildcard-allow policy before the first turn), run
against ``claude_code`` or ``codex``, e.g.::

    CLIO_RUN_LIVE=1 CLIO_GACT_FIXTURE_PORT=18997 \
      uv run pytest tests/test_real_cases/test_earthscope_interactive.py \
        -k a2ui_idle_selection --provider claude_code --model haiku \
        -o addopts="" -p no:cacheprovider -q

and likewise with ``-k a2ui_queued_selection`` / ``-k a2ui_waiting_user_selection``
(and ``--provider codex --model gpt-5-codex`` for the second required cell).
Each scene installs the pack fresh via ``marketplace_source`` (the SAME
mechanism ``ClioAgent.invoke`` already uses for a workspace-scoped
``/v1/agent-blueprints/install``, real CTE, real gact server — no fixture
stand-in), so no server-side pre-provisioning is required beyond the
``clio-kit`` tool servers the sibling ``earthscope-gnss-region`` scenes above
already depend on.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest

BLUEPRINT = "earthscope-gnss-region"
CASE_DIR = "benchmark/case02-earthscope-csv-seismic-geography"

# --- S7 A2UI live gate: a different, single-expert pack (owns the catalog) ----

A2UI_BLUEPRINT = "earthscope-single-agent"
A2UI_PACK_ROOT = (
    Path(__file__).resolve().parents[2]
    / "external"
    / "clio-agent-marketplace"
    / "earthscope-single-agent"
)
A2UI_CATALOG_ID = "https://iowarp.ai/a2ui/catalogs/earthscope-stations/v1"
A2UI_HEADERS = {"X-GACT-Version": "0.3", "X-A2UI-Version": "0.9.1"}
A2UI_PROMPT = "Show me the GNSS stations around Los Angeles."
A2UI_ASK_FIRST_PROMPT = (
    "Show me the GNSS stations around Los Angeles, and check with me before "
    "doing anything else with them."
)


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


# =============================================================================
# S7 A2UI live gate (deliverable 5): idle / queued / waiting-user selection
# =============================================================================


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _a2ui_selected_action(
    surface_id: str, search_id: str, station_ids: list[str]
) -> dict[str, Any]:
    return {
        "version": "v0.9.1",
        "action": {
            "name": "earthscope.stations.selected",
            "surfaceId": surface_id,
            "sourceComponentId": "confirmButton",
            "timestamp": _now_iso(),
            "context": {"searchId": search_id, "stationIds": station_ids},
        },
    }


def _a2ui_surface(http: httpx.Client, session_id: str) -> dict[str, Any]:
    """Return this session's ``earthscope-stations`` surface wire snapshot."""

    surfaces = http.get(f"/v1/sessions/{session_id}/a2ui/surfaces").json()["surfaces"]
    matches = [s for s in surfaces if s.get("catalog_id") == A2UI_CATALOG_ID]
    assert matches, f"no {A2UI_CATALOG_ID} surface found for session {session_id}: {surfaces}"
    return matches[-1]


def _fold_a2ui_surface(surface: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict]]:
    """Fold a surface's ordered official messages into (data_model, components).

    A minimal client-side fold (createSurface/updateComponents/updateDataModel,
    in order) — exactly the state a real A2UI renderer would hold, read here
    instead of a second maintained copy of what the agent produced.
    """

    data_model: dict[str, Any] = {}
    components: dict[str, dict[str, Any]] = {}
    for message in surface.get("messages") or []:
        update_components = message.get("updateComponents")
        if isinstance(update_components, dict):
            for component in update_components.get("components") or []:
                cid = component.get("id")
                if cid:
                    components[str(cid)] = component
        update_data_model = message.get("updateDataModel")
        if isinstance(update_data_model, dict):
            path = str(update_data_model.get("path") or "/")
            if path == "/":
                value = update_data_model.get("value")
                if isinstance(value, dict):
                    data_model = value
            elif "value" in update_data_model:
                key = path.strip("/").split("/")[0]
                if key:
                    data_model[key] = update_data_model["value"]
    return data_model, components


def _a2ui_station_ids(surface: dict[str, Any], count: int = 2) -> tuple[str, list[str]]:
    """Return ``(searchId, the first ``count`` station ids)`` straight off the
    live surface's own data model (or its ``StationMap`` points as a fallback)
    — never ids the harness invents."""

    data_model, components = _fold_a2ui_surface(surface)
    search_id = str(data_model.get("searchId") or "")
    ids = [str(s.get("id")) for s in (data_model.get("stations") or []) if s.get("id")]
    if len(ids) < count:
        station_map = next(
            (c for c in components.values() if c.get("component") == "StationMap"), None
        )
        if station_map:
            mapped = [str(p.get("id")) for p in station_map.get("points") or [] if p.get("id")]
            ids = mapped if len(mapped) > len(ids) else ids
    assert len(ids) >= count, (
        f"surface data model/components name fewer than {count} station ids: {data_model}"
    )
    return search_id, ids[:count]


def _wait_a2ui_session_status(
    http: httpx.Client, session_id: str, targets: set[str], *, no_progress_s: float = 900.0
) -> dict[str, Any]:
    """Progress-aware wait for the session's ``status`` to land in ``targets``.

    Mirrors ``ClioAgent._post_turn``'s no-progress watchdog (the server owns
    turn-timeout detection; the client only guards against the server itself
    going dark) instead of a blind sleep or a hard wall-clock cap.
    """

    start = time.monotonic()
    last_status = ""
    last_change = start
    while True:
        session = http.get(f"/v1/sessions/{session_id}").json()
        status = str(session.get("status") or "")
        now = time.monotonic()
        if status != last_status:
            last_status, last_change = status, now
        if status in targets:
            return session
        if now - last_change > no_progress_s:
            raise TimeoutError(
                f"session {session_id} stuck in status={status!r} for {no_progress_s:g}s "
                f"(waiting for one of {sorted(targets)})"
            )
        time.sleep(1.0)


def _a2ui_fresh_tool_calls(
    http: httpx.Client, session_id: str, seen_ids: set[str]
) -> list[dict[str, Any]]:
    """Tool calls from every message NOT in ``seen_ids``, via the SAME
    extraction ``ClioAgent`` uses for every other scene in this file (deferred
    import: ``clio_sut`` imports ``agent_test`` at module level, which is only
    installed where the live tier actually runs — see conftest.py)."""

    from .clio_sut import ClioAgent  # noqa: PLC0415 - agent_test-gated, live-only

    snapshot = http.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
    fresh = [m for m in snapshot if str(m.get("id")) not in seen_ids]
    tool_calls, _steps, _cost, _structured = ClioAgent._extract_messages(fresh)
    return [{"name": tc.name, "args": tc.args, "output": tc.output} for tc in tool_calls]


def _a2ui_install_and_start(
    agent: Any, gact_server: Any, tmp_path: Path, label: str, prompt: str
) -> tuple[Any, str]:
    """Run turn 1 (installs the pack, activates it, sends ``prompt``)."""

    run = agent.run(
        {
            "task": prompt,
            "blueprint_id": A2UI_BLUEPRINT,
            "marketplace_source": str(A2UI_PACK_ROOT),
            "workdir": str(tmp_path),
            "trace_path": str(gact_server.trace_dir / f"{label}.run.jsonl"),
            "timeout_s": 0,
        }
    )
    assert run.error is None, run.error
    assert run.extra["blueprint_activated"], run.extra.get("active_agent_blueprint_id")
    return run, str(run.extra["session_id"])


@pytest.mark.real_case
@pytest.mark.live
def test_earthscope_a2ui_idle_selection(agent: Any, gact_server: Any, tmp_path: Path) -> None:
    """Idle: the turn ends with the surface ready; the posted selection starts
    a fresh turn whose tool calls stage exactly those two stations."""

    _run, session_id = _a2ui_install_and_start(
        agent, gact_server, tmp_path, "a2ui_idle_selection", A2UI_PROMPT
    )

    with httpx.Client(base_url=gact_server.url, timeout=200.0) as http:
        surface = _a2ui_surface(http, session_id)
        assert surface["state"] == "ready", surface
        search_id, station_ids = _a2ui_station_ids(surface)

        seen_ids = {
            str(m.get("id"))
            for m in http.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
        }
        action = _a2ui_selected_action(surface["id"], search_id, station_ids)
        posted = http.post(
            f"/v1/sessions/{session_id}/a2ui/actions", headers=A2UI_HEADERS, json={"message": action}
        )
        assert posted.status_code == 200, posted.text
        assert posted.json()["delivery"] == "start"

        _wait_a2ui_session_status(http, session_id, {"idle"})
        tool_calls = _a2ui_fresh_tool_calls(http, session_id, seen_ids)

    staged = _staged({"tool_calls": tool_calls})
    assert set(staged) == set(station_ids), (staged, station_ids)


@pytest.mark.real_case
@pytest.mark.live
def test_earthscope_a2ui_queued_selection(agent: Any, gact_server: Any, tmp_path: Path) -> None:
    """Queued: the selection is posted while a follow-up turn is still
    running — one durable record, delivered by steer, consumed once at the
    running turn's next drain point."""

    _run, session_id = _a2ui_install_and_start(
        agent, gact_server, tmp_path, "a2ui_queued_selection", A2UI_PROMPT
    )

    with httpx.Client(base_url=gact_server.url, timeout=200.0) as http:
        surface = _a2ui_surface(http, session_id)
        assert surface["state"] == "ready", surface
        search_id, station_ids = _a2ui_station_ids(surface)

        seen_ids = {
            str(m.get("id"))
            for m in http.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
        }
        # A follow-up prompt that keeps the session busy long enough to race
        # the action in while it is still running -- not awaited here.
        follow_up = http.post(
            f"/v1/sessions/{session_id}/messages",
            json={
                "parts": [
                    {
                        "type": "text",
                        "text": "Before we continue, summarize what you found so far.",
                    }
                ]
            },
        )
        follow_up.raise_for_status()
        _wait_a2ui_session_status(http, session_id, {"running"}, no_progress_s=60.0)

        action = _a2ui_selected_action(surface["id"], search_id, station_ids)
        first = http.post(
            f"/v1/sessions/{session_id}/a2ui/actions", headers=A2UI_HEADERS, json={"message": action}
        )
        assert first.status_code == 200, first.text
        assert first.json()["delivery"] == "steer"
        action_id = first.json()["action_id"]

        duplicate = http.post(
            f"/v1/sessions/{session_id}/a2ui/actions", headers=A2UI_HEADERS, json={"message": action}
        )
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json()["action_id"] == action_id

        _wait_a2ui_session_status(http, session_id, {"idle"})
        tool_calls = _a2ui_fresh_tool_calls(http, session_id, seen_ids)
        settled = _a2ui_surface(http, session_id)

    matching = [a for a in settled.get("actions") or [] if a.get("id") == action_id]
    assert len(matching) == 1, settled.get("actions")
    assert matching[0]["state"] == "consumed", matching[0]
    staged = _staged({"tool_calls": tool_calls})
    assert set(station_ids) <= set(staged), (staged, station_ids)


@pytest.mark.real_case
@pytest.mark.live
def test_earthscope_a2ui_waiting_user_selection(
    agent: Any, gact_server: Any, tmp_path: Path
) -> None:
    """Waiting-user: the agent ends its turn asking which stations to
    analyse, ``surface_id``-bound; the posted selection resumes that exact
    question and the resumed turn's tool calls stage those two stations."""

    _run, session_id = _a2ui_install_and_start(
        agent, gact_server, tmp_path, "a2ui_waiting_user_selection", A2UI_ASK_FIRST_PROMPT
    )

    with httpx.Client(base_url=gact_server.url, timeout=200.0) as http:
        session = http.get(f"/v1/sessions/{session_id}").json()
        assert session.get("status") == "waiting_user", (
            "agent completed without pausing to ask which stations to analyse: "
            f"status={session.get('status')!r}"
        )
        surface = _a2ui_surface(http, session_id)
        assert surface["state"] == "ready", surface
        search_id, station_ids = _a2ui_station_ids(surface)

        seen_ids = {
            str(m.get("id"))
            for m in http.get(f"/v1/sessions/{session_id}/messages").json()["messages"]
        }
        action = _a2ui_selected_action(surface["id"], search_id, station_ids)
        posted = http.post(
            f"/v1/sessions/{session_id}/a2ui/actions", headers=A2UI_HEADERS, json={"message": action}
        )
        assert posted.status_code == 200, posted.text
        assert posted.json()["delivery"] == "resolve_question"

        _wait_a2ui_session_status(http, session_id, {"idle"})
        tool_calls = _a2ui_fresh_tool_calls(http, session_id, seen_ids)

    staged = _staged({"tool_calls": tool_calls})
    assert set(staged) == set(station_ids), (staged, station_ids)
