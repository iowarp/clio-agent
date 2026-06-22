#!/usr/bin/env python3
"""Experiment 2: does model choice fix Tier-1 routing? Run the 8 advisory
prompts through the full ClioAgent on several Groq models and tally how often
the planner delegates to the HDF5 expert (route 'hdf5') versus self-answers
('chat'). Routing is the planner LM's decision, so this measures whether some
model delegates reliably without any architecture change."""
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))
import dspy
from clio_agent.config import setup_dspy, load_project_env_file
from clio_agent.agent import ClioAgent

PROMPTS = {
    "amortization": "I need to read a large HDF5 file many times in an analysis loop. Should I bundle it into clio-core / IOWarp once so my repeated reads are faster than reading it with h5py each time?",
    "consolidation": "I'm about to ingest an HDF5 file with thousands of small datasets into clio-core. Are there performance concerns, and how should I prepare the file?",
    "when_to_use": "When is it actually worth ingesting an HDF5 file into clio-core instead of just reading it with h5py?",
    "api_usage": "How do I ingest an HDF5 file into clio-core and then read one dataset's array back out?",
    "one_time_read": "Is it worth loading my HDF5 file into clio-core if I only need to read it once?",
    "object_count": "Does the number of datasets in my HDF5 file affect how fast clio-core can ingest and serve it?",
    "sharing_fit": "I need several processes to read the same HDF5 data at once. Is clio-core a good fit for that?",
    "big_single_reread": "I have one large 10 GB HDF5 dataset I re-read constantly. Will clio-core make those reads faster than h5py?",
}

MODELS = [
    "llama-3.3-70b-versatile",
    "qwen/qwen3-32b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "groq/compound",
]


def safe(m):
    return m.replace("/", "_").replace(".", "")


def run_model(model: str) -> Counter:
    os.environ["CLIO_LM_MODEL"] = model
    load_project_env_file()  # groq base+key from .env (does not override CLIO_LM_MODEL)
    lm = setup_dspy(verbose=False)
    dspy.configure(lm=lm, adapter=dspy.ChatAdapter())
    dd = f"/tmp/arc_sweep_{safe(model)}"
    shutil.rmtree(dd, ignore_errors=True)
    agent = ClioAgent(verbose=False, data_dir=dd)  # router LM picks up CLIO_LM_MODEL here
    c = Counter()
    for pid, q in PROMPTS.items():
        try:
            pred = agent.forward(q, session_id=f"{safe(model)}_{pid}")
            route = getattr(pred, "selected_expert", None) or "?"
        except Exception as e:  # noqa: BLE001
            route = f"err:{type(e).__name__}"
        c[route] += 1
        print(f"  [{model}] {pid:18} -> {route}")
    return c


def main():
    results = {}
    for m in MODELS:
        print(f"\n=== {m} ===")
        try:
            results[m] = run_model(m)
        except Exception as e:  # noqa: BLE001
            print(f"  MODEL FAILED: {type(e).__name__}: {e}")
            results[m] = Counter({"model_error": len(PROMPTS)})
    n = len(PROMPTS)
    print(f"\n{'='*60}\nDELEGATION RATE (route=hdf5) per model, out of {n}\n{'='*60}")
    for m in MODELS:
        c = results[m]
        print(f"  {m:42} hdf5 {c.get('hdf5',0)}/{n}   {dict(c)}")


if __name__ == "__main__":
    main()
