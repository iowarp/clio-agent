"""H7: ``h7-trail``, a lab's records to trace a QC failure back to its cause.

A long-horizon task that one script cannot answer: the cause is reached through a
chain of documents where each hop is named only in the previous document's prose (an
instrument, a date, a technician, a lamp lot, a vendor notice), among several hundred
routine notes and two plausible branches that are within specification. Reading the
right documents in order is the work; a keyword sweep finds the start and the
distractors, not the chain.

Layout::

    h7-trail/
      README.md                 what the folders hold
      qc/batch-<id>.md          QC reports (one failed)
      notes/<yyyy>/<mm>/<id>.md lab notebook entries, random ids
      instruments/<name>.csv    instrument logs: date, event, by, ref
      vendor/notices/<id>.md    supplier notices
      samples.csv               the sample registry

The chain (each hop named only in the text before it): the failed QC report names the
assay and its date; the sample registry gives the instrument; the instrument log shows
a maintenance event two days before, with a note reference; the note records a lamp
replacement and the lamp's lot; a vendor notice recalls that lot for wavelength drift,
which explains the low readings. Distractors: a temperature excursion on the batch's
storage freezer (logged, within specification) and a pipette recalibration on
another instrument the same week.
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from scripts.eval.tool_calling.fixtures.common import derive_rng, reset_dir, write_text

FIXTURE = "h7-trail"
#: Routine notebook entries around the chain (the volume that rules out reading all).
ROUTINE_NOTES = 300

_TOPICS = (
    "buffer preparation", "calibration check", "sample intake", "plate reader warm-up",
    "freezer inventory", "pipette check", "centrifuge balance", "reagent lot change",
    "training session", "waste pickup", "incubator log", "instrument cleaning",
)  # fmt: skip
_TECHS = ("J. Ortiz", "M. Chen", "R. Ahmed", "S. Novak", "L. Haddad", "P. Iyer")
_INSTRUMENTS = ("SPEC-1", "SPEC-2", "SPEC-3", "PIP-7", "FRZ-2", "FRZ-4")


def _note_id(rng: random.Random) -> str:
    return f"{rng.randrange(16**6):06x}"


def _note(day: date, author: str, title: str, body: str) -> str:
    return f"# {title}\n\nDate: {day.isoformat()}\nAuthor: {author}\n\n{body}\n"


def write_trail(dest: Path, seed: int) -> dict[str, Any]:
    """Write the records under ``dest``; return the H7 answer key."""
    rng = derive_rng(seed, "H7")
    root = reset_dir(dest)
    start = date(2026, 9, 1)
    days = [start + timedelta(days=d) for d in range(60)]
    batch = f"B-{rng.randrange(10, 90)}"
    assay_day = days[41]
    service_day = assay_day - timedelta(days=2)
    lot = f"L-{rng.randrange(1000, 9999)}"
    service_note = _note_id(rng)
    notice = f"VN-{rng.randrange(100, 999)}"
    tech = "R. Ahmed"

    write_text(
        root / "README.md",
        "# Lab records export\n\nQC reports (`qc/`), lab notebook entries (`notes/`), "
        "instrument logs (`instruments/`), supplier notices (`vendor/notices/`) and the "
        "sample registry (`samples.csv`).\n",
    )
    # QC reports: one failed, the rest pass.
    for _ in range(20):
        other = f"B-{rng.randrange(10, 90)}"
        if other == batch:
            continue
        write_text(
            root / "qc" / f"batch-{other}.md",
            f"# QC report, batch {other}\n\nAssay: absorbance at 340 nm. Result: pass.\n",
        )
    write_text(
        root / "qc" / f"batch-{batch}.md",
        f"# QC report, batch {batch}\n\nAssay: absorbance at 340 nm, run on "
        f"{assay_day.isoformat()}. Result: FAIL. Readings were 18% below the reference "
        "standard across all wells. Storage and handling records should be reviewed.\n",
    )
    # Sample registry: the batch's assay instrument appears only here.
    root.mkdir(parents=True, exist_ok=True)
    with (root / "samples.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["batch", "assay_date", "instrument", "freezer"])
        others = [f"B-{n}" for n in range(10, 90) if f"B-{n}" != batch]
        for _ in range(80):
            d = rng.choice(days)
            writer.writerow([rng.choice(others), d, rng.choice(_INSTRUMENTS[:3]),
                             rng.choice(("FRZ-2", "FRZ-4"))])  # fmt: skip
        writer.writerow([batch, assay_day.isoformat(), "SPEC-3", "FRZ-4"])
    # Instrument logs: routine rows plus the chain's maintenance row and two distractors.
    logs: dict[str, list[list[str]]] = {name: [] for name in _INSTRUMENTS}
    for name in _INSTRUMENTS:
        for d in days[::3]:
            logs[name].append([d.isoformat(), "daily check", rng.choice(_TECHS), ""])
    logs["SPEC-3"].append(
        [service_day.isoformat(), "maintenance: source replaced", tech, f"note {service_note}"]
    )
    excursion_note = _note_id(rng)
    logs["FRZ-4"].append(
        [(assay_day - timedelta(days=5)).isoformat(), "temperature alarm", "M. Chen",
         f"note {excursion_note}"]
    )  # fmt: skip
    pipette_note = _note_id(rng)
    logs["PIP-7"].append(
        [(assay_day - timedelta(days=3)).isoformat(), "recalibrated", "S. Novak",
         f"note {pipette_note}"]
    )  # fmt: skip
    (root / "instruments").mkdir(parents=True, exist_ok=True)
    for name, rows in logs.items():
        rows.sort()
        with (root / "instruments" / f"{name}.csv").open("w", newline="", encoding="utf-8") as h:
            w = csv.writer(h)
            w.writerow(["date", "event", "by", "ref"])
            w.writerows(rows)
    notes = root / "notes"

    def put(note_id: str, day: date, text: str) -> None:
        write_text(notes / f"{day:%Y}" / f"{day:%m}" / f"{note_id}.md", text)

    put(service_note, service_day, _note(
        service_day, tech, "SPEC-3 source replacement",
        f"The deuterium lamp on SPEC-3 failed its intensity check and was replaced from "
        f"stores (lamp lot {lot}). Warm-up and blank were normal afterwards.",
    ))  # fmt: skip
    put(excursion_note, assay_day - timedelta(days=5), _note(
        assay_day - timedelta(days=5), "M. Chen", "FRZ-4 alarm",
        "Door left ajar for 6 minutes; peak -71 C against a -70 C limit for 2 minutes. "
        "Within the documented tolerance; no samples affected.",
    ))  # fmt: skip
    put(pipette_note, assay_day - timedelta(days=3), _note(
        assay_day - timedelta(days=3), "S. Novak", "PIP-7 recalibration",
        "Gravimetric check at 10, 100 and 1000 uL; all within 0.5%.",
    ))  # fmt: skip
    for _ in range(ROUTINE_NOTES):
        d = rng.choice(days)
        topic = rng.choice(_TOPICS)
        put(_note_id(rng), d, _note(d, rng.choice(_TECHS), topic.capitalize(),
            f"Routine {topic}. Nothing unusual. Instrument {rng.choice(_INSTRUMENTS)} used."))  # fmt: skip
    # Vendor notices: one recalls the lamp lot.
    for k in range(12):
        write_text(
            root / "vendor" / "notices" / f"VN-{rng.randrange(100, 999)}-{k}.md",
            f"# Supplier notice\n\nRoutine documentation update for catalogue item "
            f"{rng.randrange(1000, 9999)}. No action required.\n",
        )
    write_text(
        root / "vendor" / "notices" / f"{notice}.md",
        f"# Supplier notice {notice}: deuterium lamp recall\n\nLamps from lot {lot} emit "
        "with a wavelength drift of up to 4 nm after installation, which lowers absorbance "
        "readings near 340 nm by 15-20%. Replace affected lamps and repeat assays run "
        "since installation.\n",
    )
    return {
        "batch": batch,
        "instrument": "SPEC-3",
        "service_day": service_day.isoformat(),
        "lot": lot,
        "notice": notice,
        "cause_terms": ["recall", "drift", "wavelength"],
        "distractors": {
            "freezer": ["FRZ-4", "temperature", "freezer"],
            "pipette": ["PIP-7", "pipette"],
        },
    }
