"""Score a study's law against the hidden law on settings the agent never measured.

Takes the law the merged Conclusion states (``--formula``, or the formula of the
hypothesis the latest Conclusion's claims rest on, from a study snapshot) and
evaluates it against 50 held-out settings across the full range, regime included,
drawn from the same secret seed (``CLIO_SECRET_BENCH_SEED``). It reports, as facts:

* the prediction error per setting and its RMS, in units of the stated noise of one
  period (the reading's noise divided by the ten cycles);
* whether the law gives the decoy input no effect, and what the Conclusion says;
* the threshold the law uses for the regime input (a constant compared with it) next
  to the true one;
* the same error for the textbook pendulum law, the baseline a recalled answer gives.

A formula is an expression over the apparatus inputs (``arm_length_m``,
``bob_mass_kg``, ``release_angle_deg``, ``ambient_temp_c``; ``L``, ``m``, ``theta`` and
``T_c`` are accepted too) giving seconds per cycle (``--quantity ten_cycles`` when it
gives the time for ten). ``^`` is a power; ``sqrt``, ``exp``, ``log``, ``log10``,
``abs``, ``min``, ``max``, ``pi``, ``where(cond, a, b)`` and ``a if cond else b`` are
allowed. A leading ``T =`` is dropped.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "external/clio-agent-marketplace/plugins/hidden-law-bench/impl/src"))

from hidden_law_bench.law import (  # noqa: E402
    CYCLES,
    INPUTS,
    NOISE_S,
    draw_law,
    held_out_settings,
    textbook_period,
)

ALIASES = {
    "L": "arm_length_m",
    "length": "arm_length_m",
    "arm_length": "arm_length_m",
    "m": "bob_mass_kg",
    "mass": "bob_mass_kg",
    "bob_mass": "bob_mass_kg",
    "theta": "release_angle_deg",
    "angle": "release_angle_deg",
    "release_angle": "release_angle_deg",
    "T_c": "ambient_temp_c",
    "temp": "ambient_temp_c",
    "temperature": "ambient_temp_c",
    "ambient_temp": "ambient_temp_c",
}
FUNCTIONS = {
    "sqrt": math.sqrt,
    "exp": math.exp,
    "log": math.log,
    "ln": math.log,
    "log10": math.log10,
    "abs": abs,
    "min": min,
    "max": max,
    "pow": pow,
    "where": lambda cond, a, b: a if cond else b,
}
_ALLOWED = (
    ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name, ast.Load, ast.Constant,
    ast.Compare, ast.IfExp, ast.BoolOp, ast.And, ast.Or, ast.Add, ast.Sub, ast.Mult,
    ast.Div, ast.Pow, ast.USub, ast.UAdd, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq,
    ast.NotEq, ast.Mod,
)  # fmt: skip


class Formula:
    """A safely evaluated expression over the apparatus inputs."""

    def __init__(self, text: str) -> None:
        source = re.sub(r"^\s*[A-Za-z_]\w*\s*=(?!=)", "", text).strip()
        self.text = text
        self.tree = ast.parse(source.replace("^", "**").replace("π", "pi"), mode="eval")
        for node in ast.walk(self.tree):
            if not isinstance(node, _ALLOWED):
                raise ValueError(f"formula uses {type(node).__name__}, which is not allowed")
            if isinstance(node, ast.Call) and not (
                isinstance(node.func, ast.Name) and node.func.id in FUNCTIONS
            ):
                raise ValueError("formula calls a function that is not allowed")
            if isinstance(node, ast.Name) and node.id not in FUNCTIONS:
                name = ALIASES.get(node.id, node.id)
                if name not in INPUTS and name not in {"pi", "e", "g"}:
                    raise ValueError(f"formula names {node.id!r}, which is not an input")
        self.code = compile(self.tree, "<formula>", "eval")

    def __call__(self, settings: dict[str, float]) -> float:
        names: dict[str, Any] = {**FUNCTIONS, "pi": math.pi, "e": math.e, "g": 9.81}
        names.update(settings)
        names.update({alias: settings[name] for alias, name in ALIASES.items()})
        return float(eval(self.code, {"__builtins__": {}}, names))  # noqa: S307 - AST-checked

    def thresholds(self, variable: str) -> list[float]:
        """Constants this formula compares ``variable`` with (its regime thresholds)."""

        found = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Compare):
                continue
            sides = [node.left, *node.comparators]
            named = any(
                isinstance(s, ast.Name) and ALIASES.get(s.id, s.id) == variable for s in sides
            )
            if named:
                found += [float(s.value) for s in sides if isinstance(s, ast.Constant)]
        return found


def law_from_snapshot(data: dict[str, Any]) -> tuple[str, str]:
    """The formula the latest Conclusion rests on, and the Conclusion's text."""

    entries = (data.get("science_log") or {}).get("entries") or []
    by_id = {entry["id"]: entry for entry in entries}
    conclusions = [e for e in entries if e["type"] == "conclusion"]
    if not conclusions:
        raise SystemExit("the study has no Conclusion")
    conclusion = conclusions[-1]
    text = " ".join(str(c.get("text") or "") for c in conclusion["fields"].get("claims") or [])
    seen, stack = (
        set(),
        [c for claim in conclusion["fields"].get("claims") or [] for c in claim.get("cites") or []],
    )
    while stack:
        entry = by_id.get(stack.pop())
        if entry is None or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        formula = (entry.get("fields") or {}).get("formula")
        if entry["type"] == "hypothesis" and formula:
            return str(formula), text
        stack += [link["to"] for link in entry.get("links") or []]
    raise SystemExit("no hypothesis with a formula behind the Conclusion; pass --formula")


def score(formula: Formula, secret: str, quantity: str = "period") -> dict[str, Any]:
    law = draw_law(secret)
    sd_period = NOISE_S / CYCLES
    rows, errors, textbook = [], [], []
    for settings in held_out_settings(secret):
        truth = law.period(settings)
        predicted = formula(settings) / (CYCLES if quantity == "ten_cycles" else 1)
        error = (predicted - truth) / sd_period
        baseline = (textbook_period(settings) - truth) / sd_period
        errors.append(error)
        textbook.append(baseline)
        rows.append(
            {
                **{k: round(v, 4) for k, v in settings.items()},
                "true_period_s": round(truth, 5),
                "predicted_period_s": round(predicted, 5),
                "error_noise_units": round(error, 3),
                "textbook_error_noise_units": round(baseline, 3),
            }
        )
    _u, low, high, _r = INPUTS[law.decoy]
    probe = {name: (lo + hi) / 2 for name, (_x, lo, hi, _y) in INPUTS.items()}
    decoy_effect = formula({**probe, law.decoy: high}) - formula({**probe, law.decoy: low})

    def rms(values: list[float]) -> float:
        return round(math.sqrt(sum(v * v for v in values) / len(values)), 3)

    return {
        "formula": formula.text,
        "quantity": quantity,
        "held_out_settings": len(rows),
        "rms_error_noise_units": rms(errors),
        "max_abs_error_noise_units": round(max(abs(e) for e in errors), 3),
        "textbook_rms_error_noise_units": rms(textbook),
        "decoy_input": law.decoy,
        "law_gives_decoy_no_effect": abs(decoy_effect) < 1e-9,
        "regime_input": law.regime,
        "true_threshold": round(law.threshold, 4),
        "law_thresholds_for_regime_input": formula.thresholds(law.regime),
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--formula", default="")
    parser.add_argument("--snapshot", default="", help="a study snapshot JSON")
    parser.add_argument("--quantity", choices=["period", "ten_cycles"], default="period")
    parser.add_argument("--out", required=True, help="output JSON path")
    args = parser.parse_args()
    conclusion = ""
    text = args.formula
    if not text:
        text, conclusion = law_from_snapshot(json.loads(Path(args.snapshot).read_text()))
    report = score(Formula(text), os.environ.get("CLIO_SECRET_BENCH_SEED", ""), args.quantity)
    report["conclusion"] = conclusion
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    main()
