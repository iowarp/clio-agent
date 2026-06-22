#!/usr/bin/env python3
"""Tier-1 (orchestrator) advisory A/B for the hdf5-clio-core-ingest skill.

Runs the full ClioAgent (planner -> delegates to HDF5 expert -> skill fires)
and A/Bs the agent's final answer with the skill present vs absent. Isolation:
fresh ARC data_dir per arm, unique session_id per prompt, skill dir moved out
for the 'without' arm. Shows the skill helps only when the planner delegates.

Run: CLIO_LM_MODEL=openai/gpt-oss-120b CLIO_ALLOWED_ROOTS="/tmp:/home/matthewlarson" \
       <clio-agent>/.venv/bin/python e2e_advisory_ab.py
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))

import dspy  # noqa: E402
from clio_agent.config import setup_dspy, load_project_env_file  # noqa: E402
from clio_agent.experts import hdf5_skills  # noqa: E402
from clio_agent.agent import ClioAgent  # noqa: E402

SKILL_NAME = "hdf5-clio-core-ingest"
_SKILLS_ROOT = Path(hdf5_skills.__file__).resolve().parent
_SKILL_DIR = _SKILLS_ROOT / SKILL_NAME
_STASH = _SKILLS_ROOT.parent / f"_stashed_{SKILL_NAME}"

PROMPTS = [
    {"id": "amortization",
     "q": ("I need to read a large HDF5 file many times in an analysis loop. "
           "Should I bundle it into clio-core / IOWarp once so my repeated reads "
           "are faster than reading it with h5py each time?"),
     "rubric": {
         "says NOT faster / won't help read speed":
             [r"\bnot\b.{0,25}(faster|speed)", r"won'?t be faster", r"\bslower\b",
              r"no(t)?.{0,15}(speed|performance).{0,15}(benefit|gain|improvement)",
              r"will not.{0,10}(faster|speed)"],
         "no amortization / break-even": [r"amorti", r"break.?even", r"does ?n.?t pay off"],
         "prefer native h5py for speed": [r"h5py", r"read.{0,10}(nativ|directly)"],
         "value is sharing/tiering not speed":
             [r"shar(e|ing|ed)", r"\btier", r"concurren", r"multiple (readers|consumers|processes)"],
     }},
    {"id": "consolidation",
     "q": ("I'm about to ingest an HDF5 file with thousands of small datasets "
           "into clio-core. Are there performance concerns, and how should I "
           "prepare the file?"),
     "rubric": {
         "consolidate / fewer larger datasets":
             [r"consolidat", r"fewer.{0,15}dataset", r"combin.{0,15}dataset",
              r"merg.{0,15}dataset", r"pack.{0,15}(dataset|into)", r"single (big |large )?dataset"],
         "per-object / object-count cost":
             [r"per.?object", r"object count", r"number of dataset", r"many small",
              r"thousands of", r"per.?dataset"],
         "cost magnitude (slow/overhead/x)":
             [r"\bslow", r"overhead", r"\d+\s*[x×]", r"penalt", r"minutes", r"expensive", r"linear"],
     }},
    {"id": "when_to_use",
     "q": ("When is it actually worth ingesting an HDF5 file into clio-core "
           "instead of just reading it with h5py?"),
     "rubric": {
         "default to native h5py": [r"default.{0,15}h5py", r"just (use |read ).{0,5}h5py",
                                    r"read.{0,10}(nativ|directly)", r"unless"],
         "not for read speed": [r"\bnot\b.{0,25}(faster|speed|latency)", r"never.{0,15}amorti",
                                r"does ?n.?t.{0,15}(speed|faster)"],
         "value = sharing/tiering/multi-consumer":
             [r"shar(e|ing|ed)", r"\btier", r"concurren", r"multiple (readers|consumers|processes)",
              r"residen"],
     }},
    {"id": "api_usage",
     "q": ("How do I ingest an HDF5 file into clio-core and then read one "
           "dataset's array back out?"),
     "rubric": {
         "hdf5:: routing (src prefix)": [r"hdf5::", r"src.{0,15}prefix", r"protocol prefix"],
         "context_bundle for ingest": [r"context_bundle", r"\bbundle\b"],
         "CTE Tag.GetBlob for read": [r"getblob", r"\.get_?blob", r"data ?plane", r"\bcte\b"],
         "avoid context_retrieve for binary": [r"not.{0,25}context_retrieve",
                                               r"context_retrieve.{0,30}(text|utf|not)"],
     }},
    {"id": "one_time_read",
     "q": ("Is it worth loading my HDF5 file into clio-core if I only need to "
           "read it once?"),
     "rubric": {
         "no / not worth for a single read":
             [r"\bno\b", r"not worth", r"only.{0,10}once", r"one.?time", r"single read"],
         "ingest cost not recovered": [r"ingest.{0,15}cost", r"amorti", r"overhead",
                                       r"pay.{0,12}ingest", r"upfront"],
         "default to native h5py": [r"h5py", r"read.{0,10}(nativ|directly)"],
     }},
    {"id": "object_count",
     "q": ("Does the number of datasets in my HDF5 file affect how fast "
           "clio-core can ingest and serve it?"),
     "rubric": {
         "yes, scales with dataset count":
             [r"\byes\b", r"scale.{0,15}(count|number|dataset)", r"per.?object",
              r"per.?dataset", r"\blinear", r"number of dataset"],
         "many small = penalty/slow":
             [r"many small", r"thousands", r"\bslow", r"penalt", r"\d+\s*[x×]", r"overhead"],
         "consolidate": [r"consolidat", r"fewer.{0,15}dataset", r"combin", r"merg"],
     }},
    {"id": "sharing_fit",
     "q": ("I need several processes to read the same HDF5 data at once. Is "
           "clio-core a good fit for that?"),
     "rubric": {
         "yes - sharing/concurrent is the right use":
             [r"\byes\b", r"good fit", r"shar", r"concurren", r"simultaneous",
              r"multiple (process|reader|consumer)"],
         "shared-memory / residency / tiering":
             [r"shared.?memory", r"residen", r"\btier", r"in.?memory", r"blob store"],
     }},
    {"id": "big_single_reread",
     "q": ("I have one large 10 GB HDF5 dataset I re-read constantly. Will "
           "clio-core make those reads faster than h5py?"),
     "rubric": {
         "no / not faster": [r"\bno\b", r"\bnot\b.{0,25}(faster|speed)", r"\bslower\b", r"won'?t"],
         "per-chunk cost / doesn't amortize at scale":
             [r"per.?chunk", r"amorti", r"\bnever\b", r"chunk.{0,15}overhead", r"large.{0,15}(slow|overhead)"],
         "native h5py faster": [r"h5py", r"nativ"],
     }},
]


def _score(text: str, rubric: dict) -> dict:
    low = text.lower()
    return {c: bool(any(re.search(p, low) for p in pats)) for c, pats in rubric.items()}


def _run_arm(label: str, data_dir: str) -> dict:
    hdf5_skills._skill_index.cache_clear()
    agent = ClioAgent(verbose=False, data_dir=data_dir)
    out = {}
    for p in PROMPTS:
        would_inject = (hdf5_skills.match_skills(p["q"], top_k=1) or [("<none>", 0)])[0][0]
        try:
            pred = agent.forward(p["q"], session_id=f"{label}_{p['id']}")
            answer = str(getattr(pred, "answer", "") or "")
            selected = getattr(pred, "selected_expert", None)
        except Exception as exc:  # noqa: BLE001
            answer, selected = f"<ERROR: {exc}>", None
        scored = _score(answer, p["rubric"])
        out[p["id"]] = {"selected_expert": selected, "would_inject_skill": would_inject,
                        "answer": answer, "scored": scored,
                        "hits": sum(scored.values()), "total": len(scored)}
        print(f"\n{'='*78}\n[{label}] {p['id']}  (route={selected}, "
              f"skill-if-delegated={would_inject})\n{'-'*78}")
        print(answer[:1200])
        print(f"  -> rubric {out[p['id']]['hits']}/{out[p['id']]['total']}: "
              + ", ".join(f"{'✓' if v else '✗'} {k}" for k, v in scored.items()))
    return out


def main() -> None:
    load_project_env_file()
    lm = setup_dspy(verbose=True)
    dspy.configure(lm=lm, adapter=dspy.ChatAdapter())

    results = {}
    try:
        if _SKILL_DIR.exists():
            shutil.move(str(_SKILL_DIR), str(_STASH))
        shutil.rmtree("/tmp/arc_ab_without", ignore_errors=True)
        print("\n########## ARM: WITHOUT skill (Tier-1 orchestrator) ##########")
        results["without"] = _run_arm("without", "/tmp/arc_ab_without")
    finally:
        if _STASH.exists():
            shutil.move(str(_STASH), str(_SKILL_DIR))

    shutil.rmtree("/tmp/arc_ab_with", ignore_errors=True)
    print("\n########## ARM: WITH skill (Tier-1 orchestrator) ##########")
    results["with"] = _run_arm("with", "/tmp/arc_ab_with")

    print(f"\n{'='*78}\nSUMMARY — Tier-1 orchestrator A/B, rubric hits (without -> with)\n{'='*78}")
    for p in PROMPTS:
        wo, wi = results["without"][p["id"]], results["with"][p["id"]]
        print(f"  {p['id']:<16} {wo['hits']}/{wo['total']} -> {wi['hits']}/{wi['total']}"
              f"   (route {wo['selected_expert']} -> {wi['selected_expert']})")

    Path("e2e_advisory_ab_results.json").write_text(json.dumps(results, indent=2))
    print("\nwrote e2e_advisory_ab_results.json")


if __name__ == "__main__":
    main()
