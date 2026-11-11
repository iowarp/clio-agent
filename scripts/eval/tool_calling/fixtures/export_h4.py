"""H4 and H5: ``h4-export``, a growth-chamber experiment export to investigate.

An OPAL-shaped stand-in: a broad question over a staged instrument export of
140 files (about 300 MB at full scale), too large to read whole but easy to
stream or sample. Trial ``PFR-26`` grows tomato seedlings in four chambers
(``GC1`` to ``GC4``) under two light conditions, A (white LED) and B (white LED
enriched with far-red at the same PAR), in six three-day campaigns; each
campaign runs two chambers per condition and the assignment alternates. Each
run folder, ``data/<date>/<chamber>/cond<A|B>/run_<id>/``, holds:

- ``run.json``: sidecar metadata (units, instruments, start time, notes);
- ``env.csv``: chamber environment every 10 s (temperature, humidity, CO2,
  PAR, far-red);
- ``gas_exchange.h5``: net assimilation ``A_net`` per plant at 1 Hz (the bulk);
- ``growth.parquet``: per-plant height and leaf area from imaging every 15 min;
- ``instrument.log``: the chamber controller's log.

Seeded facts (the answer key; the effect is computed from the generated data):

- ``calibration``: one run's gas analyser failed its zero check; the only record
  is a WARN line in its ``instrument.log``. Its ``A_net`` carries a positive
  offset, so respiration in darkness reads positive.
- ``unit``: one run's ``growth.parquet`` stores height in cm (its field metadata
  says so) while its ``run.json`` declares mm, as every other run uses.
- ``duplicate``: one run was exported twice; the copy has a new id in the same
  chamber and date folder, byte-identical data files, and is absent from the
  index and the campaign files.
- ``clock``: one run's environment logger ran on the chamber panel's clock
  (UTC+01:00) while its rows say UTC, so every ``env.csv`` timestamp is one
  hour late against its sidecar and its gas exchange time axis; lights appear
  to switch on at 07:00 instead of 06:00 UTC.
- the effect: B plants elongate faster (about 8.4 against 6.0 mm/day); no other
  measured variable differs by condition except the far-red light itself.
- the decoy: one run has a three-hour dark interval mid-photoperiod (PAR zero,
  negative ``A_net``), which the README and its sidecar explain as the planned
  dark adaptation of protocol section 5.2.

Noise: routine WARN lines, a chamber that runs warm, a plant removed mid-run,
and the stale README and index of :mod:`export_h4_layout`.
"""

from __future__ import annotations

import json
import random
from datetime import timedelta
from pathlib import Path
from typing import Any

import h5py
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from scripts.eval.tool_calling.fixtures.common import derive_rng, write_text
from scripts.eval.tool_calling.fixtures.export_h4_layout import (
    CAMPAIGNS,
    CHAMBERS,
    CLOCK_SHIFT_S,
    CO2_SETPOINT,
    DECOY_DARK,
    ELONGATION,
    EXPERIMENT,
    FAR_RED,
    FIRST_DAY,
    IMAGING_MIN,
    LABELS,
    LIGHTS,
    PAR_LIGHT,
    PLANTS,
    RUN_FILES,
    RUN_HOURS,
    UNITS,
    Run,
    calibration_json,
    campaign_json,
    chambers_csv,
    export_log,
    index_csv,
    iso,
    plan_runs,
    protocol_md,
    readme_md,
    run_id,
)

FIXTURE = "h4-export"
_NOISE_WARNINGS = (
    "WARN  door opened for {n} s (maintenance round)",
    "WARN  humidifier reservoir low, refill requested",
    "WARN  imaging frame dropped for {plant}, retried",
    "WARN  cuvette fan at {n} % of nominal speed, recovered",
)
#: What was measured, for an ungraded recall like P4's (a fact counts when any term is named).
MEASURED = [
    {"fact": "net assimilation", "match_any": ["assimilation", "A_net", "photosynth"]},
    {"fact": "plant height", "match_any": ["height", "elongation"]},
    {"fact": "leaf area", "match_any": ["leaf area", "leaf_area"]},
    {"fact": "CO2", "match_any": ["CO2", "CO₂", "co2_ppm"]},
    {"fact": "PAR", "match_any": ["PAR", "photosynthetically active"]},
    {"fact": "far-red", "match_any": ["far-red", "far red", "far_red"]},
    {"fact": "air temperature", "match_any": ["temperature", "air_temp"]},
    {"fact": "humidity", "match_any": ["humidity", "rh_pct", "RH"]},
]

Heights = dict[str, tuple[np.ndarray, np.ndarray]]


def _light_mask(run: Run, seconds: np.ndarray) -> np.ndarray:
    """Lights on, by seconds since the run start (runs start at 00:00 UTC)."""
    hours = (seconds % 86400) / 3600.0
    light = (hours >= LIGHTS[0]) & (hours < LIGHTS[1])
    if "decoy" in run.roles:
        day = seconds // 86400
        light &= ~((day == 1) & (hours >= DECOY_DARK[0]) & (hours < DECOY_DARK[1]))
    return light


def _gas_exchange(run: Run, np_rng: np.random.Generator, period_s: int, path: Path) -> None:
    seconds = np.arange(0, RUN_HOURS * 3600, period_s, dtype=np.int64)
    light = _light_mask(run, seconds)
    capacity = np_rng.normal(13.5, 1.0, size=(PLANTS, 1))
    noise_light = np_rng.normal(0.0, 0.6, size=(PLANTS, seconds.size))
    noise_dark = np_rng.normal(0.0, 0.25, size=(PLANTS, seconds.size))
    a_net = np.where(light, capacity + noise_light, -1.6 + noise_dark) + run.calibration_offset
    epoch = run.start.timestamp() + seconds.astype(np.float64)
    with h5py.File(path, "w", libver="earliest") as handle:
        handle.attrs["experiment"] = EXPERIMENT
        handle.attrs["run_id"] = run.run_id
        handle.attrs["chamber"] = run.chamber
        handle.attrs["instrument"] = f"GA-{run.index}"
        group = handle.create_group("gas_exchange")
        group.attrs["description"] = "per-plant leaf cuvettes on the chamber IRGA"
        stamp = group.create_dataset("time", data=epoch)
        stamp.attrs["units"] = "seconds since 1970-01-01T00:00:00Z"
        values = group.create_dataset("A_net", data=a_net.astype(np.float32))
        values.attrs["units"] = UNITS["A_net"]
        values.attrs["long_name"] = "net CO2 assimilation"
        values.attrs["dims"] = "plant, time"
        names = [f"P{n:02d}" for n in range(1, PLANTS + 1)]
        group.create_dataset("plant_id", data=np.array(names, dtype="S3"))


def _env_csv(run: Run, np_rng: np.random.Generator, period_s: int) -> str:
    seconds = np.arange(0, RUN_HOURS * 3600, period_s, dtype=np.int64)
    light = _light_mask(run, seconds)
    n = seconds.size
    warm = 0.5 if run.chamber == "GC4" else 0.0
    temp = 21.0 + warm + 1.0 * light + np_rng.normal(0, 0.15, n)
    rh = 65.0 + np_rng.normal(0, 2.0, n)
    co2 = CO2_SETPOINT + np_rng.normal(0, 10.0, n)
    par = np.where(light, PAR_LIGHT + np_rng.normal(0, 8.0, n), 0.0)
    far_red = np.where(light, FAR_RED[run.condition] * (1 + np_rng.normal(0, 0.02, n)), 0.0)
    shift = CLOCK_SHIFT_S if "clock" in run.roles else 0
    rows = ["timestamp,air_temp_c,rh_pct,co2_ppm,par_umol_m2_s,far_red_umol_m2_s"]
    for i in range(n):
        stamp = iso(run.start + timedelta(seconds=int(seconds[i]) + shift))
        rows.append(f"{stamp},{temp[i]:.2f},{rh[i]:.1f},{co2[i]:.1f},{par[i]:.1f},{far_red[i]:.1f}")
    return "\n".join(rows) + "\n"


def _growth(run: Run, np_rng: np.random.Generator) -> tuple[pa.Table, Heights]:
    """The imaging table and, per plant, ``(days, height in mm)`` for the answer key."""
    seconds = np.arange(0, RUN_HOURS * 3600, IMAGING_MIN * 60, dtype=np.int64)
    seconds = seconds[_light_mask(run, seconds)]
    initial = np_rng.normal(40.0, 4.0, PLANTS)
    rates = ELONGATION[run.condition] + np_rng.normal(0.0, 0.6, PLANTS)
    leaf0 = np_rng.normal(12.0, 1.5, PLANTS)
    unit = "cm" if "unit" in run.roles else "mm"
    columns: dict[str, list[Any]] = {"timestamp": [], "plant_id": [], "height": [], "leaf_area": []}
    truth: Heights = {}
    start = int(run.start.timestamp())
    for p in range(PLANTS):
        plant = f"P{p + 1:02d}"
        at = seconds if plant != run.removed_plant else seconds[seconds < 86400]
        days = at / 86400.0
        height = np.round(initial[p] + rates[p] * days + np_rng.normal(0, 0.4, days.size), 1)
        leaf = np.round(leaf0[p] + 2.5 * days + np_rng.normal(0, 0.3, days.size), 1)
        truth[plant] = (days, height)
        columns["timestamp"] += [start + int(s) for s in at]
        columns["plant_id"] += [plant] * days.size
        columns["height"] += list(np.round(height / 10.0, 2) if unit == "cm" else height)
        columns["leaf_area"] += list(leaf)
    schema = pa.schema(
        [
            pa.field("timestamp", pa.timestamp("s", tz="UTC")),
            pa.field("plant_id", pa.string()),
            pa.field("height", pa.float64(), metadata={"unit": unit}),
            pa.field("leaf_area", pa.float64(), metadata={"unit": "cm2"}),
        ],
        metadata={"run_id": run.run_id, "source": f"CAM-{run.index}"},
    )
    return pa.table(columns, schema=schema), truth


def _log(run: Run, rng: random.Random) -> str:
    tag = run.index
    clock = "chamber panel clock (UTC+01:00)" if "clock" in run.roles else "NTP pool.ntp.org"
    profile = "legacy" if "unit" in run.roles else "default"
    lines: list[tuple[int, str]] = [
        (0, f"INFO  run {run.run_id} start chamber={run.chamber} condition={run.condition} "
            f"analyser=GA-{tag} logger=EL-{tag} camera=CAM-{tag}"),
        (2, f"INFO  EL-{tag} clock source: {clock}"),
        (4, f"INFO  CAM-{tag} imaging export profile: {profile}"),
    ]  # fmt: skip
    if "calibration" in run.roles:
        lines.append(
            (5, f"WARN  GA-{tag} zero check failed: reference gas read "
                f"+{run.calibration_offset:.2f} umol m-2 s-1; offset not corrected in "
                "exported A_net (service ticket SR-2231)")
        )  # fmt: skip
    else:
        lines.append((5, f"INFO  GA-{tag} zero check passed (0.00 +/- 0.05 umol m-2 s-1)"))
    for day in range(RUN_HOURS // 24):
        lines.append((day * 86400 + LIGHTS[0] * 3600, "INFO  lights on (schedule)"))
        lines.append((day * 86400 + LIGHTS[1] * 3600, "INFO  lights off (schedule)"))
    if "decoy" in run.roles:
        lines.append((86400 + DECOY_DARK[0] * 3600, "INFO  dark adaptation start (protocol 5.2)"))
        lines.append((86400 + DECOY_DARK[1] * 3600, "INFO  dark adaptation end (protocol 5.2)"))
    if run.removed_plant:
        lines.append((86400 - 600, f"INFO  plant {run.removed_plant} removed (stem damage)"))
    for hour in range(1, RUN_HOURS):
        flow = 500 + rng.randint(-4, 4)
        lines.append((hour * 3600 + 30, f"INFO  GA-{tag} heartbeat flow={flow} ml/min"))
    for _ in range(rng.randint(2, 4)):
        text = rng.choice(_NOISE_WARNINGS).format(
            n=rng.randint(20, 90), plant=f"P{rng.randint(1, PLANTS):02d}"
        )
        lines.append((rng.randint(3600, (RUN_HOURS - 1) * 3600), text))
    lines.append((RUN_HOURS * 3600 - 1, f"INFO  run {run.run_id} complete"))
    lines.sort(key=lambda item: item[0])
    return "".join(f"{iso(run.start + timedelta(seconds=s))} {text}\n" for s, text in lines)


def _sidecar(run: Run, rid: str, exported_at: str, ge_s: int, env_s: int) -> str:
    notes = []
    if "decoy" in run.roles:
        day = (run.start + timedelta(days=1)).strftime("%Y-%m-%d")
        notes.append(
            f"Scheduled dark-adaptation interval {DECOY_DARK[0]:02d}:00-{DECOY_DARK[1]:02d}:00 "
            f"UTC on {day} for chlorophyll fluorescence imaging (protocol 5.2). Lights off "
            "and negative A_net in that interval are expected; not a fault."
        )
    if run.removed_plant:
        notes.append(f"{run.removed_plant} removed after day 1 (stem damage).")
    sidecar = {
        "experiment": EXPERIMENT,
        "run_id": rid,
        "campaign": f"C{run.campaign}",
        "chamber": run.chamber,
        "condition": run.condition,
        "condition_label": LABELS[run.condition],
        "start_utc": iso(run.start),
        "end_utc": iso(run.start + timedelta(hours=RUN_HOURS)),
        "duration_h": RUN_HOURS,
        "plants": [f"P{n:02d}" for n in range(1, PLANTS + 1)],
        "instruments": {
            "gas_analyser": f"GA-{run.index}",
            "env_logger": f"EL-{run.index}",
            "camera": f"CAM-{run.index}",
        },
        "sampling": {"gas_exchange_s": ge_s, "environment_s": env_s, "imaging_min": IMAGING_MIN},
        "units": UNITS,
        "files": list(RUN_FILES),
        "exported_at": exported_at,
        "export_tool": "chamberx-export 3.2.1",
        "notes": " ".join(notes),
    }
    return json.dumps(sidecar, indent=2) + "\n"


def _np_rng(seed: int, run: Run, part: str) -> np.random.Generator:
    """One stream per run and file, so imaging (and so the effect) does not depend on scale."""
    return np.random.default_rng(derive_rng(seed, f"H4:{run.run_id}:{part}").getrandbits(64))


def _write_run(root: Path, run: Run, seed: int, ge_s: int, env_s: int, exported: str) -> Heights:
    folder = root / run.rel
    folder.mkdir(parents=True, exist_ok=True)
    _gas_exchange(run, _np_rng(seed, run, "gas"), ge_s, folder / "gas_exchange.h5")
    write_text(folder / "env.csv", _env_csv(run, _np_rng(seed, run, "env"), env_s))
    table, truth = _growth(run, _np_rng(seed, run, "growth"))
    pq.write_table(table, folder / "growth.parquet", compression="snappy")
    write_text(folder / "instrument.log", _log(run, derive_rng(seed, f"H4-log:{run.run_id}")))
    write_text(folder / "run.json", _sidecar(run, run.run_id, exported, ge_s, env_s))
    return truth


def plant_growth(days: np.ndarray, height_mm: np.ndarray) -> dict[str, float]:
    """One plant: least-squares elongation rate (mm/day), height gain and final height (mm)."""
    return {
        "rate": float(np.polyfit(days, height_mm, 1)[0]),
        "gain": float(height_mm[-1] - height_mm[0]),
        "final": float(height_mm[-1]),
    }


_METRICS = (
    ("elongation rate", "rate", "mm/day"),
    ("height gain over the run", "gain", "mm"),
    ("final height", "final", "mm"),
)


def effect_key(plants: list[tuple[str, dict[str, float]]]) -> dict[str, Any]:
    """Condition medians over every plant of the real runs, for three growth metrics."""
    metrics = []
    for name, key, unit in _METRICS:
        med = {c: round(float(np.median([g[key] for k, g in plants if k == c])), 3) for c in "AB"}
        diff = med["B"] - med["A"]
        metrics.append(
            {
                "name": name,
                "unit": unit,
                "A": med["A"],
                "B": med["B"],
                "difference": round(diff, 3),
                "relative": round(diff / med["A"], 4),
            }
        )
    return {"variable": "stem height (growth.parquet)", "direction": "B>A", "metrics": metrics}


#: How an answer names each finding, as case-insensitive regular expressions the grader
#: looks for beside the run (``grade_export``); any one term counts.
TERMS = {
    "calibration": [
        r"zero[- ]?check", r"calibrat", r"\boffset", r"reference gas", r"\bbias",
        r"SR-2231", r"positive (?:A_net|assimilation|net assimilation|respiration|values?)",
    ],
    "unit": [
        r"\bcm\b", r"centimet", r"\btenth", r"ten times", r"10\s*[x×]", r"factor of (?:10|ten)\b",
        r"(?:wrong|different|mismatch\w*|inconsistent)\s+units?\b",
        r"\bunits?\s+(?:mismatch|error|inconsisten|confusion)",
    ],
    "duplicate": [
        r"duplicat", r"\bcop(?:y|ies|ied)\b", r"identical", r"\btwice\b",
        r"re-?export", r"exported again", r"same data",
    ],
    "clock": [
        r"\bclock", r"time ?zone", r"local time", r"\bCET\b", r"UTC\s*\+\s*0?1",
        r"\+01:00", r"one hour", r"\b1\s*h(?:ou)?r?\b", r"60 min", r"\bshift",
        r"\boffset", r"\blag", r"\blate\b",
    ],
    "index": [
        r"MANIFEST", r"\bindex", r"\blisted\b", r"\bunlisted\b", r"\bmissing\b",
        r"\babsent\b", r"does not exist", r"\bno (?:such )?(?:run )?folder", r"phantom",
        r"never exported", r"not exported",
    ],
}  # fmt: skip
#: The decoy is flagged when the answer names its run and its dark interval as a fault,
#: with nothing saying the interval was planned.
DECOY_INTERVAL = [
    r"\bdark", r"\blights?\b", r"\bPAR\b", r"\blamps?\b", r"photoperiod", r"outage",
    r"\bpower\b", r"10:00", r"13:00",
]  # fmt: skip
DECOY_FAULT = [
    r"\bfault", r"\bfail", r"malfunction", r"outage", r"\banomal", r"glitch", r"\berror",
    r"\bproblem", r"\bissue", r"unexplained", r"unexpected", r"suspicious", r"\bexclud",
    r"\bdiscard", r"\bdrop(?:ped)?\b", r"power (?:cut|loss|failure)", r"\blost\b",
]  # fmt: skip
DECOY_EXPLAINED = [
    r"\bplanned", r"\bscheduled", r"\bexpected", r"dark[- ]adaptation", r"\bprotocol",
    r"by design", r"\bintentional", r"\bdeliberate", r"fluorescence", r"Fv/Fm",
    r"\bdocumented", r"not an? (?:fault|problem|issue|anomaly|error)",
    r"\bno (?:fault|problem|issue)",
]  # fmt: skip


def _where(run: Run) -> dict[str, Any]:
    """Every way an answer may name ``run``: its id, or its chamber with a date it ran
    on (any of its three days) or its campaign."""
    days = [(run.start + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(RUN_HOURS // 24)]
    return {
        "runs": [run.run_id],
        "chamber": run.chamber,
        "campaign": run.campaign,
        "date": run.date,
        "dates": days,
        "path": run.rel,
    }


def _anomaly(kind: str, run: Run, evidence: list[str], discover: str, **extra: Any) -> dict:
    return {"id": kind, **_where(run), **extra, "terms": TERMS[kind], "evidence": evidence,
            "discover": discover}  # fmt: skip


def write_export(dest: Path, seed: int, *, scale: int = 1) -> dict[str, Any]:
    """Write the export under ``dest``; return the H4/H5 answer key.

    ``scale`` divides the sampling rates (gas exchange every ``scale`` s,
    environment every ``10 * scale`` s) and must divide 360. Tests use a large
    scale; every seeded fact holds at any scale.
    """
    if scale < 1 or 360 % scale:
        raise ValueError("scale must divide 360")
    ge_s, env_s = scale, 10 * scale
    runs, picked = plan_runs(derive_rng(seed, "H4"))
    dest.mkdir(parents=True, exist_ok=True)
    export_day = FIRST_DAY + timedelta(days=7 * CAMPAIGNS + 1)
    plants: list[tuple[str, dict[str, float]]] = []
    for n, run in enumerate(runs):
        exported = iso(export_day + timedelta(minutes=10 + 3 * n))
        truth = _write_run(dest, run, seed, ge_s, env_s, exported)
        plants += [(run.condition, plant_growth(*pair)) for pair in truth.values()]
    original = picked["duplicate"]
    copy_id = run_id(len(runs))
    phantom = run_id(len(runs) + 1)
    copy_dir = dest / original.folder / copy_id
    copy_dir.mkdir(parents=True)
    for name in RUN_FILES[1:]:
        (copy_dir / name).write_bytes((dest / original.rel / name).read_bytes())
    copied = iso(export_day + timedelta(minutes=10 + 3 * len(runs) + 12))
    write_text(copy_dir / "run.json", _sidecar(original, copy_id, copied, ge_s, env_s))

    write_text(dest / "README.md", readme_md(runs, picked))
    write_text(dest / "PROTOCOL.md", protocol_md())
    write_text(dest / "MANIFEST.csv", index_csv(runs, picked, phantom, env_s, ge_s))
    write_text(dest / "site" / "chambers.csv", chambers_csv())
    for i, chamber in enumerate(CHAMBERS, start=1):
        write_text(dest / "calibration" / f"{chamber}.json", calibration_json(chamber, i))
    for campaign in range(1, CAMPAIGNS + 1):
        members = [r for r in runs if r.campaign == campaign]
        write_text(dest / "campaigns" / f"C{campaign}.json", campaign_json(campaign, members))
    write_text(dest / "export" / "export_log.txt", export_log(runs, original, copy_id, export_day))

    cal, unit, clock, decoy = (picked[k] for k in ("calibration", "unit", "clock", "decoy"))
    unlisted = picked["unlisted"]
    dark_day = (decoy.start + timedelta(days=1)).strftime("%Y-%m-%d")
    return {
        "experiment": EXPERIMENT,
        "scale": scale,
        "sampling": {"gas_exchange_s": ge_s, "environment_s": env_s, "imaging_min": IMAGING_MIN},
        "runs": len(runs),
        "run_folders": len(runs) + 1,
        "anomalies": [
            _anomaly(
                "calibration",
                cal,
                [f"{cal.rel}/instrument.log", f"{cal.rel}/gas_exchange.h5"],
                "the zero-check WARN in its log; A_net positive in darkness",
                offset=cal.calibration_offset,
            ),
            _anomaly(
                "unit",
                unit,
                [f"{unit.rel}/growth.parquet", f"{unit.rel}/run.json"],
                "heights a tenth of every other run's; parquet field metadata says cm, the "
                "sidecar mm",
            ),
            _anomaly(
                "duplicate",
                original,
                [f"{original.rel}/gas_exchange.h5", f"{original.folder}/{copy_id}"],
                "byte-identical data files and the same start in one chamber; the copy is "
                "missing from MANIFEST.csv and the campaign file; the export retried",
                runs=[original.run_id, copy_id],
                copy=f"{original.folder}/{copy_id}",
            ),
            _anomaly(
                "clock",
                clock,
                [f"{clock.rel}/env.csv", f"{clock.rel}/run.json"],
                "env.csv starts an hour after start_utc and the h5 time axis; lights on at "
                "07:00; the log's logger clock source is the chamber panel (UTC+01:00)",
                shift_s=CLOCK_SHIFT_S,
            ),
        ],
        "decoy": {
            **_where(decoy),
            "dark_utc": [
                f"{dark_day}T{DECOY_DARK[0]:02d}:00:00Z",
                f"{dark_day}T{DECOY_DARK[1]:02d}:00:00Z",
            ],
            "interval": DECOY_INTERVAL,
            "fault": DECOY_FAULT,
            "explained": DECOY_EXPLAINED,
            "explained_in": ["README.md", f"{decoy.rel}/run.json"],
            "discover": "three dark hours on day 2: the planned dark adaptation (protocol 5.2)",
        },  # fmt: skip
        "effect": effect_key(plants),
        #: Recorded, not graded: the index lists a run never exported and omits a real one.
        "index": {
            "phantom": {"runs": [phantom], "path": f"MANIFEST.csv: {phantom}"},
            "unlisted": _where(unlisted),
            "terms": TERMS["index"],
        },
        "measured": MEASURED,
    }
