#!/usr/bin/env python3
"""Tier-2 A/B: does the `hdf5-clio-core-ingest` skill improve the HDF5 expert's
advice? Calls the expert directly on clio-core advisory prompts (no .h5 path ->
skill-synthesis path), twice: 'without' (skill dir moved out of the bundle,
index cache cleared) vs 'with'. Same local LM both arms; answers scored against
an empirical rubric and saved to skill_ab_results.json.

Run: CLIO_LM_PROVIDER=ollama CLIO_LM_MODEL=granite3.1-dense:8b CLIO_LM_TEMPERATURE=0 \
       <clio-agent>/.venv/bin/python skill_ab_demo.py
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

# --- locate clio-agent/src and make clio_agent importable ----------------
_REPO = Path(__file__).resolve().parents[2]          # iowarp_repos/
_SRC = _REPO / "src"
sys.path.insert(0, str(_SRC))

import dspy  # noqa: E402
from clio_agent.config import setup_dspy  # noqa: E402
from clio_agent.experts import hdf5_skills  # noqa: E402
from clio_agent.experts.hdf5_expert import HDF5Expert  # noqa: E402

SKILL_NAME = "hdf5-clio-core-ingest"
_SKILLS_ROOT = Path(hdf5_skills.__file__).resolve().parent
_SKILL_DIR = _SKILLS_ROOT / SKILL_NAME
# stash OUTSIDE the bundle root so _skill_index() (iterdir of the root) can't see it
_STASH = _SKILLS_ROOT.parent / f"_stashed_{SKILL_NAME}"

PROMPTS = [
    {
        "id": "amortization",
        "q": ("I need to read a large HDF5 file many times in an analysis loop. "
              "If I bundle it into clio-core / IOWarp once, will my repeated "
              "reads then be faster than reading the file with h5py each time?"),
        "rubric": {
            "says NOT faster": [r"\bnot\b.{0,25}(faster|speed)", r"won'?t be faster",
                                r"\bslower\b", r"no(t)?.{0,15}(speed|performance).{0,15}(benefit|gain|improvement)",
                                r"unlikely.{0,15}faster", r"will not.{0,10}(faster|speed)"],
            "no amortization / break-even": [r"amorti", r"break.?even", r"does ?n.?t pay off",
                                             r"never (pays|wins)"],
            "prefer native h5py for speed": [r"h5py", r"read.{0,10}(nativ|directly)"],
            "value is sharing/tiering not speed": [r"shar(e|ing|ed)", r"\btier", r"concurren",
                                                   r"multiple (readers|consumers|processes)"],
        },
    },
    {
        "id": "consolidation",
        "q": ("I'm about to ingest an HDF5 file with thousands of small datasets "
              "into clio-core. Are there performance concerns, and how should I "
              "prepare the file?"),
        "rubric": {
            "consolidate / fewer larger datasets": [r"consolidat", r"fewer.{0,15}dataset",
                                                    r"combin.{0,15}dataset", r"merg.{0,15}dataset",
                                                    r"pack.{0,15}(dataset|into)", r"single (big |large )?dataset"],
            "per-object / object-count cost": [r"per.?object", r"object count",
                                               r"number of dataset", r"many small",
                                               r"thousands of", r"per.?dataset"],
            "cost magnitude (slow/overhead/x)": [r"\bslow", r"overhead", r"\d+\s*[x×]",
                                                 r"penalt", r"minutes", r"expensive", r"linear"],
        },
    },
    {
        "id": "api_usage",
        "q": ("How do I ingest an HDF5 file into clio-core and then read one "
              "dataset's array back out?"),
        "rubric": {
            "hdf5:: routing (src prefix)": [r"hdf5::", r"src.{0,15}prefix",
                                            r"protocol prefix", r"prefix.{0,15}select"],
            "context_bundle for ingest": [r"context_bundle", r"\bbundle\b"],
            "CTE Tag.GetBlob for read": [r"getblob", r"\.get_?blob", r"data ?plane", r"\bcte\b tag",
                                         r"tag\b.{0,40}blob"],
            "avoid context_retrieve for binary": [r"not.{0,25}context_retrieve",
                                                  r"context_retrieve.{0,30}(text|utf|not)"],
        },
    },
]


def _score(text: str, rubric: dict) -> dict:
    low = text.lower()
    return {crit: bool(any(re.search(p, low) for p in pats))
            for crit, pats in rubric.items()}


def _run_arm(label: str) -> dict:
    hdf5_skills._skill_index.cache_clear()  # rebuild index for current dir state
    expert = HDF5Expert()
    out = {}
    for p in PROMPTS:
        injected = (hdf5_skills.match_skills(p["q"], top_k=1) or [("<none>", 0)])[0][0]
        try:
            pred = expert.forward(question=p["q"], file_context="")
            analysis = str(getattr(pred, "analysis", "") or "")
            recs = str(getattr(pred, "recommendations", "") or "")
        except Exception as exc:  # noqa: BLE001
            analysis, recs = f"<ERROR: {exc}>", ""
        text = (analysis + "\n" + recs).strip()
        scored = _score(text, p["rubric"])
        out[p["id"]] = {"injected_skill": injected, "text": text,
                        "scored": scored, "hits": sum(scored.values()),
                        "total": len(scored)}
        print(f"\n{'='*78}\n[{label}] {p['id']}  (injected skill: {injected})\n{'-'*78}")
        print(text[:1600])
        print(f"  -> rubric {out[p['id']]['hits']}/{out[p['id']]['total']}: "
              + ", ".join(f"{'✓' if v else '✗'} {k}" for k, v in scored.items()))
    return out


def main() -> None:
    lm = setup_dspy(verbose=True)
    dspy.configure(lm=lm, adapter=dspy.ChatAdapter())

    results = {}
    try:
        # WITHOUT: stash the skill dir outside the bundle root
        if _SKILL_DIR.exists():
            shutil.move(str(_SKILL_DIR), str(_STASH))
        print("\n########## ARM: WITHOUT skill ##########")
        results["without"] = _run_arm("without")
    finally:
        if _STASH.exists():  # always restore
            shutil.move(str(_STASH), str(_SKILL_DIR))

    print("\n########## ARM: WITH skill ##########")
    results["with"] = _run_arm("with")

    # ---- summary delta ----
    print(f"\n{'='*78}\nSUMMARY — rubric hits (without -> with)\n{'='*78}")
    for p in PROMPTS:
        wo = results["without"][p["id"]]
        wi = results["with"][p["id"]]
        print(f"  {p['id']:<16} {wo['hits']}/{wo['total']}  ->  {wi['hits']}/{wi['total']}"
              f"   (skill: {wo['injected_skill']} -> {wi['injected_skill']})")

    Path("skill_ab_results.json").write_text(json.dumps(results, indent=2))
    print("\nwrote skill_ab_results.json")


if __name__ == "__main__":
    main()
