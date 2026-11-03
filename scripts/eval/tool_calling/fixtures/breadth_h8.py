"""H8: ``h8-breadth``, twelve QC failures in a lab's records, each with its own cause.

H7 showed that one chain, however deep, is short work on a strong model (8 steps): the
agent reads the candidates in parallel and follows the chain. Long work comes from
breadth, many independent investigations, which is also where delegating to explore
children should pay. Each failed batch here has its own cause, reachable only through
its own chain, among routine notes and an in-spec distractor on each batch.

Cause kinds (cycled over the batches; the key names the evidence each needs):

* ``lamp``: the batch's spectrophotometer had its lamp replaced (instrument log, note
  with the lamp lot) and a supplier notice recalls that lot;
* ``standard``: the reference standard used (registry) had expired before the assay
  (``standards.csv``);
* ``reagent``: the reagent lot used (registry) is named in a supplier notice for
  swapped concentration labels;
* ``freezer``: the batch's freezer had a real excursion during its storage window (log
  alarm, note with an out-of-spec peak);
* ``pipette``: the pipette used failed its gravimetric check before the batch was
  prepared (log, note with the error);
* ``swap``: an intake note records that the batch's labels were swapped with another's.
"""

from __future__ import annotations

import csv
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from scripts.eval.tool_calling.fixtures.common import derive_rng, reset_dir, write_text

FIXTURE = "h8-breadth"
BATCHES = 12
ROUTINE_NOTES = 400
KINDS = ("lamp", "standard", "reagent", "freezer", "pipette", "swap")
#: Terms an answer uses for each cause, beside the cause's own identifier.
CAUSE_TERMS = {
    "lamp": ["recall", "lamp"],
    "standard": ["expired", "expiry"],
    "reagent": ["label", "swapped", "concentration"],
    "freezer": ["excursion", "out of spec", "thaw", "-4", "temperature"],
    "pipette": ["pipette", "tolerance", "gravimetric", "error"],
    "swap": ["swap", "mix", "mislabel", "label"],
}
_TECHS = ("J. Ortiz", "M. Chen", "R. Ahmed", "S. Novak", "L. Haddad", "P. Iyer")


def _id(rng: random.Random) -> str:
    return f"{rng.randrange(16**6):06x}"


def _note(day: date, author: str, title: str, body: str) -> str:
    return f"# {title}\n\nDate: {day.isoformat()}\nAuthor: {author}\n\n{body}\n"


def write_breadth(dest: Path, seed: int) -> dict[str, Any]:
    """Write the records under ``dest``; return the H8 answer key (one entry per batch)."""
    rng = derive_rng(seed, "H8")
    root = reset_dir(dest)
    start = date(2026, 7, 1)
    days = [start + timedelta(days=d) for d in range(120)]
    notes: list[tuple[str, date, str]] = []
    logs: dict[str, list[list[str]]] = {}
    notices: list[tuple[str, str]] = []
    standards: list[list[str]] = []
    registry: list[list[str]] = []
    key: list[dict[str, Any]] = []
    numbers = rng.sample(range(100, 999), BATCHES + 60)
    failed = [f"B-{n}" for n in numbers[:BATCHES]]
    passed = [f"B-{n}" for n in numbers[BATCHES:]]

    def log(name: str, day: date, event: str, by: str, ref: str = "") -> None:
        logs.setdefault(name, []).append([day.isoformat(), event, by, ref])

    for i, batch in enumerate(failed):
        kind = KINDS[i % len(KINDS)]
        day = days[20 + i * 7]
        spec, freezer, pipette = f"SPEC-{i % 4 + 1}", f"FRZ-{i % 3 + 1}", f"PIP-{i % 5 + 1}"
        reagent, std = f"R-{rng.randrange(1000, 9999)}", f"STD-{rng.randrange(100, 999)}"
        expires = (day + timedelta(days=90)).isoformat()
        entry: dict[str, Any] = {"batch": batch, "kind": kind}
        if kind == "lamp":
            lot, note, notice = (
                f"L-{rng.randrange(1000, 9999)}",
                _id(rng),
                f"VN-{rng.randrange(100, 999)}",
            )
            log(
                spec,
                day - timedelta(days=2),
                "maintenance: source replaced",
                "R. Ahmed",
                f"note {note}",
            )
            notes.append(
                (
                    note,
                    day - timedelta(days=2),
                    _note(
                        day - timedelta(days=2),
                        "R. Ahmed",
                        f"{spec} source replacement",
                        f"Deuterium lamp replaced from stores, lot {lot}.",
                    ),
                )
            )
            notices.append(
                (
                    notice,
                    f"# Supplier notice {notice}: deuterium lamp recall\n\nLot {lot} "
                    "drifts up to 4 nm in wavelength; absorbance near 340 nm reads 15-20% low.\n",
                )
            )
            entry["ids"] = [lot, notice]
            symptom = "Readings were 18% below the reference standard across all wells."
        elif kind == "standard":
            expires = (day - timedelta(days=rng.randrange(5, 30))).isoformat()
            entry["ids"] = [std]
            symptom = (
                "Readings were 12% above the reference curve; the curve itself looked shifted."
            )
        elif kind == "reagent":
            notice = f"VN-{rng.randrange(100, 999)}"
            notices.append(
                (
                    notice,
                    f"# Supplier notice {notice}: label error\n\nReagent lot "
                    f"{reagent} was shipped with the 1 mM and 10 mM labels swapped.\n",
                )
            )
            entry["ids"] = [reagent, notice]
            symptom = "Signal was about ten times the expected range in every well."
        elif kind == "freezer":
            note = _id(rng)
            alarm = day - timedelta(days=3)
            log(freezer, alarm, "temperature alarm", "M. Chen", f"note {note}")
            notes.append(
                (
                    note,
                    alarm,
                    _note(
                        alarm,
                        "M. Chen",
                        f"{freezer} alarm",
                        "Compressor fault overnight; peak -41 C for about 5 hours against a -70 C limit. "
                        "Contents not moved until morning.",
                    ),
                )
            )
            entry["ids"] = [freezer, note]
            symptom = "Activity was 40% below the reference; degradation products were visible."
        elif kind == "pipette":
            note = _id(rng)
            check = day - timedelta(days=1)
            log(pipette, check, "gravimetric check: FAIL", "S. Novak", f"note {note}")
            notes.append(
                (
                    note,
                    check,
                    _note(
                        check,
                        "S. Novak",
                        f"{pipette} check",
                        "Error of 6% at 10 uL (limit 1%). Tagged for service, but the tag was missing "
                        "the next morning.",
                    ),
                )
            )
            entry["ids"] = [pipette, note]
            symptom = "Replicates disagreed by up to 9%; low-volume wells were worst."
        else:  # swap
            other = rng.choice(passed)
            note = _id(rng)
            notes.append(
                (
                    note,
                    day - timedelta(days=4),
                    _note(
                        day - timedelta(days=4),
                        "P. Iyer",
                        "Sample intake",
                        f"Labels for {batch} and {other} were printed in the wrong order "
                        "and applied; noticed after racking, not corrected in the registry.",
                    ),
                )
            )
            entry["ids"] = [note, other]
            symptom = "Results matched no expected profile for this sample type."
        # An in-spec distractor on one of the batch's other resources.
        dnote = _id(rng)
        dday = day - timedelta(days=rng.randrange(1, 6))
        target = freezer if kind != "freezer" else pipette
        log(
            target,
            dday,
            "alarm" if target.startswith("FRZ") else "check",
            "L. Haddad",
            f"note {dnote}",
        )
        notes.append(
            (
                dnote,
                dday,
                _note(
                    dday,
                    "L. Haddad",
                    f"{target} event",
                    "Brief deviation, within the documented tolerance; no action needed.",
                ),
            )
        )
        entry["distractor"] = target
        standards.append([std, expires])
        registry.append([batch, day.isoformat(), spec, freezer, pipette, reagent, std])
        write_text(root / "qc" / f"batch-{batch}.md", f"# QC report, batch {batch}\n\nAssay run on "
                   f"{day.isoformat()}. Result: FAIL. {symptom}\n")  # fmt: skip
        key.append(entry)

    for batch in passed:
        day = rng.choice(days)
        std = f"STD-{rng.randrange(100, 999)}"
        standards.append([std, (day + timedelta(days=120)).isoformat()])
        registry.append([batch, day.isoformat(), f"SPEC-{rng.randrange(1, 5)}",
                         f"FRZ-{rng.randrange(1, 4)}", f"PIP-{rng.randrange(1, 6)}",
                         f"R-{rng.randrange(1000, 9999)}", std])  # fmt: skip
        write_text(root / "qc" / f"batch-{batch}.md",
                   f"# QC report, batch {batch}\n\nAssay run on {day.isoformat()}. Result: pass.\n")  # fmt: skip
    for _ in range(ROUTINE_NOTES):
        day = rng.choice(days)
        notes.append((_id(rng), day, _note(day, rng.choice(_TECHS), "Routine work",
                      "Routine checks and preparation; nothing unusual.")))  # fmt: skip
    for name in (
        [f"SPEC-{n}" for n in range(1, 5)]
        + [f"FRZ-{n}" for n in range(1, 4)]
        + [f"PIP-{n}" for n in range(1, 6)]
    ):
        for day in days[::4]:
            log(name, day, "daily check", rng.choice(_TECHS))
    for k in range(20):
        notices.append((f"VN-{rng.randrange(100, 999)}-{k}", "# Supplier notice\n\nRoutine "
                        "documentation update; no action required.\n"))  # fmt: skip

    write_text(root / "README.md", "# Lab records export\n\nQC reports (`qc/`), lab notebook "
               "entries (`notes/`), instrument logs (`instruments/`), supplier notices "
               "(`vendor/notices/`), reference standards (`standards.csv`) and the sample "
               "registry (`samples.csv`).\n")  # fmt: skip
    for note_id, day, text in notes:
        write_text(root / "notes" / f"{day:%Y}" / f"{day:%m}" / f"{note_id}.md", text)
    for notice_id, text in notices:
        write_text(root / "vendor" / "notices" / f"{notice_id}.md", text)
    (root / "instruments").mkdir(parents=True, exist_ok=True)
    for name, rows in logs.items():
        with (root / "instruments" / f"{name}.csv").open("w", newline="", encoding="utf-8") as h:
            w = csv.writer(h)
            w.writerow(["date", "event", "by", "ref"])
            w.writerows(sorted(rows))
    rng.shuffle(registry)
    for path, head, rows in (
        ("samples.csv", ["batch", "assay_date", "spectrophotometer", "freezer", "pipette",
                         "reagent_lot", "standard"], registry),
        ("standards.csv", ["standard", "expires"], standards),
    ):  # fmt: skip
        with (root / path).open("w", newline="", encoding="utf-8") as h:
            w = csv.writer(h)
            w.writerow(head)
            w.writerows(rows)
    return {"batches": key, "cause_terms": CAUSE_TERMS}
