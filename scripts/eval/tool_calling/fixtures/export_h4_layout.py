"""The ``h4-export`` trial design and its documents: README, protocol, index, site files.

The run data itself is written by :mod:`export_h4`; this module holds the
design both share (chambers, campaigns, conditions, the seeded role of each
run) and the text files around the runs, which are partly wrong the way real
exports are: the README says four files per run and 24 runs, and the index
omits two run folders, lists one that was never exported and has stale counts.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

EXPERIMENT = "PFR-26"
CHAMBERS = ("GC1", "GC2", "GC3", "GC4")
CAMPAIGNS = 6
FIRST_DAY = datetime(2026, 3, 2, tzinfo=UTC)
RUN_HOURS = 72
PLANTS = 8
LIGHTS = (6, 22)  # photoperiod, UTC hours
PAR_LIGHT = 600.0
FAR_RED = {"A": 12.0, "B": 140.0}
CO2_SETPOINT = 800.0
#: Nominal stem elongation, mm/day; the answer key is computed from the files.
ELONGATION = {"A": 6.0, "B": 8.4}
IMAGING_MIN = 15
DECOY_DARK = (10, 13)  # UTC hours on the second day
CLOCK_SHIFT_S = 3600
FIRST_RUN = 101
LABELS = {"A": "control (white LED)", "B": "far-red enriched (white LED + 735 nm)"}
UNITS = {
    "air_temp": "degC",
    "rh": "%",
    "co2": "ppm",
    "par": "umol m-2 s-1",
    "far_red": "umol m-2 s-1",
    "A_net": "umol m-2 s-1",
    "height": "mm",
    "leaf_area": "cm2",
}
RUN_FILES = ("run.json", "env.csv", "gas_exchange.h5", "growth.parquet", "instrument.log")
#: Roles drawn for distinct runs: the four anomalies, the decoy and two kinds of noise.
ROLES = ("calibration", "unit", "duplicate", "clock", "decoy", "removed_plant", "unlisted")


@dataclass
class Run:
    """One run folder and its seeded roles."""

    run_id: str
    campaign: int
    chamber: str
    condition: str
    start: datetime
    roles: set[str] = field(default_factory=set)
    removed_plant: str | None = None
    calibration_offset: float = 0.0

    @property
    def date(self) -> str:
        return self.start.strftime("%Y-%m-%d")

    @property
    def folder(self) -> str:
        return f"data/{self.date}/{self.chamber}/cond{self.condition}"

    @property
    def rel(self) -> str:
        return f"{self.folder}/{self.run_id}"

    @property
    def index(self) -> int:
        return CHAMBERS.index(self.chamber) + 1


def iso(moment: datetime) -> str:
    """UTC ``YYYY-MM-DDTHH:MM:SSZ``."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def run_id(number: int) -> str:
    return f"run_{FIRST_RUN + number:04d}"


def plan_runs(rng: random.Random) -> tuple[list[Run], dict[str, Run]]:
    """The 24 real runs (campaign-major) and the run playing each seeded role."""
    runs = []
    for campaign in range(CAMPAIGNS):
        start = FIRST_DAY + timedelta(days=7 * campaign)
        for i, chamber in enumerate(CHAMBERS):
            condition = "AB"[(i + campaign) % 2]
            runs.append(Run(run_id(len(runs)), campaign + 1, chamber, condition, start))
    picked = dict(zip(ROLES, rng.sample(runs, len(ROLES)), strict=True))
    for role, run in picked.items():
        run.roles.add(role)
    picked["calibration"].calibration_offset = round(rng.uniform(2.2, 2.8), 2)
    picked["removed_plant"].removed_plant = f"P{rng.randint(1, PLANTS):02d}"
    return runs, picked


def readme_md(runs: list[Run], picked: dict[str, Run]) -> str:
    decoy = picked["decoy"]
    dark_day = (decoy.start + timedelta(days=1)).strftime("%Y-%m-%d")
    return f"""# {EXPERIMENT} growth-chamber export

Trial {EXPERIMENT} tests how far-red enrichment of white LED light changes the
growth and gas exchange of tomato seedlings (*Solanum lycopersicum* 'Moneymaker').

## Conditions

| Condition | Light | PAR | Far-red (735 nm) |
| --- | --- | --- | --- |
| A | {LABELS["A"]} | {PAR_LIGHT:.0f} umol m-2 s-1 | about {FAR_RED["A"]:.0f} umol m-2 s-1 |
| B | {LABELS["B"]} | {PAR_LIGHT:.0f} umol m-2 s-1 | about {FAR_RED["B"]:.0f} umol m-2 s-1 |

Both conditions share the photoperiod ({LIGHTS[0]:02d}:00-{LIGHTS[1]:02d}:00 UTC), CO2
({CO2_SETPOINT:.0f} ppm), humidity (65 %) and temperature (21 C dark, 22 C light).
Four chambers (GC1 to GC4) ran {CAMPAIGNS} campaigns of {RUN_HOURS} h, one a week from
{runs[0].date}, with {PLANTS} new seedlings per chamber per campaign. Each campaign runs
two chambers per condition; the assignment alternates between campaigns
(`campaigns/C<n>.json`).

## Layout

`data/<campaign start date>/<chamber>/cond<A|B>/run_<id>/` holds one run, {len(runs)} runs
in all. Each run folder holds four files:

- `run.json`: run metadata (times, instruments, units, notes);
- `env.csv`: chamber environment every 10 s;
- `gas_exchange.h5`: net assimilation `A_net` per plant (group `gas_exchange`);
- `growth.parquet`: plant height and leaf area from imaging every {IMAGING_MIN} min.

`MANIFEST.csv` indexes every exported run. `PROTOCOL.md` describes the measurements;
`calibration/` holds the sensor certificates and `site/chambers.csv` the chambers.

## Units and time

All timestamps are UTC. Units: temperature degC, humidity %, CO2 ppm, PAR and far-red
umol m-2 s-1, `A_net` umol m-2 s-1, height mm, leaf area cm2 (also in each `run.json`).

## Known events

- {dark_day}, {decoy.chamber} ({decoy.run_id}, condition {decoy.condition}): planned
  dark-adaptation interval {DECOY_DARK[0]:02d}:00-{DECOY_DARK[1]:02d}:00 UTC for chlorophyll
  fluorescence imaging (PROTOCOL.md section 5.2). Lights are off and `A_net` is negative
  in that interval by design.
- GC4 stands next to the door and runs about 0.5 C warmer than the others.
"""


def protocol_md() -> str:
    return f"""# {EXPERIMENT} protocol (v1.3)

## 1. Plant material

Tomato seedlings, 14 days after sowing, {PLANTS} per chamber per campaign (P01 to
P{PLANTS:02d}), new seedlings for every campaign.

## 2. Light treatments

Condition A: white LED. Condition B: the same white LED plus 735 nm far-red LEDs.
PAR is held at {PAR_LIGHT:.0f} umol m-2 s-1 in both, so only the far-red fraction differs.

## 3. Environment

Set points: {CO2_SETPOINT:.0f} ppm CO2, 65 % relative humidity, 21 C in the dark and 22 C
in the light. Photoperiod {LIGHTS[0]:02d}:00 to {LIGHTS[1]:02d}:00 UTC.

## 4. Measurements

4.1 Gas exchange. One leaf of each plant sits in a cuvette on the chamber's infrared
gas analyser (GA-n), sampled every second. `A_net` is net CO2 assimilation; in the
dark it is respiration and therefore negative. The analyser runs a zero check on
reference gas at the start of each run and logs the result.

4.2 Imaging. A side-view camera (CAM-n) images every plant every {IMAGING_MIN} min during the
photoperiod; height (mm) and projected leaf area (cm2) are extracted automatically.

4.3 Environment. The logger (EL-n) records air temperature, humidity, CO2, PAR and
far-red every 10 s. Logger clocks synchronise to NTP (UTC).

## 5. Interventions

5.1 Damaged plants are removed and noted in the run metadata.

5.2 Dark adaptation. Once in the trial, one chamber is darkened for three hours in
the middle of the photoperiod for chlorophyll fluorescence (Fv/Fm) imaging. The
interval is noted in the run metadata and the README.

## 6. Export

`chamberx-export` writes one folder per run with its metadata, data files and the
controller log, and appends to `export/export_log.txt`.
"""


def index_csv(runs: list[Run], picked: dict[str, Run], phantom: str, env_s: int, ge_s: int) -> str:
    """The index: omits the unlisted run (and the duplicate's copy), lists a phantom."""
    env_rows = RUN_HOURS * 3600 // env_s
    samples = RUN_HOURS * 3600 // ge_s
    rows = ["run_id,campaign,date,chamber,condition,path,n_files,env_rows,ge_samples,plants"]
    for run in runs:
        if run is picked["unlisted"]:
            continue
        rows.append(
            f"{run.run_id},C{run.campaign},{run.date},{run.chamber},{run.condition},"
            f"{run.rel},4,{env_rows},{samples},{PLANTS}"
        )
    day = (FIRST_DAY + timedelta(days=7 * CAMPAIGNS)).strftime("%Y-%m-%d")
    rows.append(
        f"{phantom},C{CAMPAIGNS + 1},{day},GC1,A,data/{day}/GC1/condA/{phantom},4,"
        f"{env_rows},{samples},{PLANTS}"
    )
    return "\n".join(rows) + "\n"


def chambers_csv() -> str:
    rows = [
        "chamber,model,room,gas_analyser,env_logger,camera,note",
        "GC1,Conviron A1000,B-114,GA-1,EL-1,CAM-1,",
        "GC2,Conviron A1000,B-114,GA-2,EL-2,CAM-2,",
        "GC3,Conviron A1000,B-115,GA-3,EL-3,CAM-3,",
        "GC4,Conviron A1000,B-115,GA-4,EL-4,CAM-4,next to the door; about 0.5 C warmer",
    ]
    return "\n".join(rows) + "\n"


def calibration_json(chamber: str, index: int) -> str:
    cert = {
        "chamber": chamber,
        "gas_analyser": {
            "id": f"GA-{index}",
            "serial": f"IRG4-{2210 + 7 * index}",
            "calibrated": f"2026-02-{10 + index:02d}",
            "valid_until": f"2026-08-{10 + index:02d}",
            "zero_offset_umol_m2_s": 0.0,
            "span_gain": round(1.0 + 0.002 * index, 3),
        },
        "env_logger": {
            "id": f"EL-{index}",
            "co2_calibrated": "2026-02-05",
            "par_sensor": f"LI-190R Q{4100 + index}",
        },
        "camera": {"id": f"CAM-{index}", "pixel_scale_mm": round(0.2 + 0.01 * index, 2)},
    }
    return json.dumps(cert, indent=2) + "\n"


def campaign_json(campaign: int, runs: list[Run]) -> str:
    data = {
        "campaign": f"C{campaign}",
        "start_utc": iso(runs[0].start),
        "duration_h": RUN_HOURS,
        "assignments": [
            {"chamber": r.chamber, "condition": r.condition, "run_id": r.run_id} for r in runs
        ],
    }
    return json.dumps(data, indent=2) + "\n"


def export_log(runs: list[Run], original: Run, copy_id: str, day: datetime) -> str:
    lines = [f"{iso(day)} chamberx-export 3.2.1 starting: {EXPERIMENT}, {len(runs)} runs queued"]
    for n, run in enumerate(runs):
        at = day + timedelta(minutes=10 + 3 * n)
        if run is original:
            lines.append(
                f"{iso(at - timedelta(minutes=1))} WARN export of {run.run_id} timed out "
                "after 600 s; retrying"
            )
        lines.append(f"{iso(at)} exported {run.run_id} -> {run.rel} (5 files)")
    at = day + timedelta(minutes=10 + 3 * len(runs) + 12)
    lines.append(f"{iso(at)} exported {copy_id} -> {original.folder}/{copy_id} (5 files)")
    lines.append(
        f"{iso(at + timedelta(minutes=1))} export complete: {len(runs) + 1} run folders, "
        f"{5 * (len(runs) + 1)} files"
    )
    return "\n".join(lines) + "\n"
