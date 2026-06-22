#!/usr/bin/env python3
"""End-to-end CAPABILITY demo: the Tier-1 ClioAgent orchestrator, driven by a
capable cloud model (Groq gpt-oss), takes an existing HDF5 file and MATERIALLY
produces a new working HDF5 file through its tool-execution layer
(hdf5_rechunk_dataset -> h5repack).

This demonstrates the multi-agent system's execute path (planner -> tool, gated
by the write-path policy). It is NOT skill-attribution: the file-creating tool
layer is skill-independent, and there is no 'ingest into clio-core' MCP tool for
the orchestrator to run — the hdf5-clio-core-ingest skill only shapes advisory
recommendations (see SKILL_AB_DEMO.md for the skill's value).

Run (clio-agent venv; .env supplies the Groq config + key):
  CLIO_ALLOWED_ROOTS="/tmp:/home/matthewlarson" \
    /path/to/clio-agent/.venv/bin/python e2e_orchestrator_demo.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "src"))

import dspy  # noqa: E402
from clio_agent.config import setup_dspy, load_project_env_file  # noqa: E402
from clio_agent.agent import ClioAgent  # noqa: E402

INPUT = str(_REPO / "translation" / "clio_core_bench" / "fixtures_200mb" / "A_contig.h5")
OUT = "/tmp/clio_e2e_rechunked.h5"
RUNNER = "/home/matthewlarson/Documents/anaconda3/envs/new_env2/bin/python"


def validate(path: str) -> dict:
    snip = textwrap.dedent(f"""
        import h5py, json
        with h5py.File({path!r}, "r") as f:
            d = f["/field"]
            print("VALID:" + json.dumps({{
                "shape": list(d.shape),
                "chunks": list(d.chunks) if d.chunks else None,
                "dtype": str(d.dtype),
            }}))
    """)
    with tempfile.TemporaryDirectory() as t:
        s = Path(t) / "v.py"
        s.write_text(snip)
        p = subprocess.run([RUNNER, str(s)], capture_output=True, text=True, timeout=30)
    for ln in p.stdout.splitlines():
        if ln.startswith("VALID:"):
            return {"valid": True, **json.loads(ln[len("VALID:"):])}
    return {"valid": False, "error": p.stderr[-300:]}


def trace_tools(agent) -> list:
    tr = getattr(agent, "_active_trace", None)
    out = []
    for t in getattr(tr, "tools", []) or []:
        out.append({"tool": getattr(t, "tool", None), "ok": getattr(t, "ok", None)})
    return out


def turn(agent, q: str, label: str) -> dict:
    print(f"\n=== {label} ===\nQ: {q}")
    pred = agent.forward(q)
    ans = str(getattr(pred, "answer", "") or "")
    sel = getattr(pred, "selected_expert", None)
    tools = trace_tools(agent)
    print(f"  selected={sel}  tools={tools}")
    print(f"  answer: {ans[:500]}")
    return {"question": q, "answer": ans, "selected": sel, "tools": tools}


def main() -> None:
    load_project_env_file()
    lm = setup_dspy(verbose=True)
    dspy.configure(lm=lm, adapter=dspy.ChatAdapter())
    Path(OUT).unlink(missing_ok=True)

    agent = ClioAgent(verbose=False, data_dir="/tmp/clio_e2e_arc")
    turns = []

    # Turn 1: imperative with the exact tool + args spelled out. For a
    # capability demo this is fair — we're exercising the execute path, not
    # testing the model's arg-inference.
    turns.append(turn(
        agent,
        (f"Call the hdf5_rechunk_dataset tool now with these exact arguments: "
         f"filepath='{INPUT}', object_path='/field', chunk_dims='50x50x50', "
         f"output_filepath='{OUT}'. Do not set chunk_adjustment or make_contiguous. "
         f"Execute it."),
        "TURN 1"))

    # Turn 2 fallback: even more explicit if no file yet.
    if not Path(OUT).exists():
        turns.append(turn(
            agent,
            (f"Use hdf5_rechunk_dataset with chunk_dims='50x50x50' (a string), "
             f"object_path='/field', filepath='{INPUT}', output_filepath='{OUT}'. "
             f"Leave all other arguments unset and run it now."),
            "TURN 2 (execution nudge)"))

    created = Path(OUT).exists()
    val = validate(OUT) if created else None
    print(f"\nRESULT: file created end-to-end? {created}  ({OUT})")
    if val:
        print("VALIDATION:", val)

    out = {"input": INPUT, "output": OUT, "created": created,
           "validation": val, "turns": turns}
    Path(_REPO / "translation" / "clio_core_bench" / "e2e_orchestrator_results.json"
         ).write_text(json.dumps(out, indent=2))
    print("wrote e2e_orchestrator_results.json")


if __name__ == "__main__":
    main()
