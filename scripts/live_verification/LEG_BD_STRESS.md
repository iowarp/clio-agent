# Leg B+D stress gate — web MCP + v2ex exerciser, ONE agent, ONE turn (#1286, C1-S6 addendum)

**Owner addendum** (same rules as the rest of `scripts/live_verification/`:
new files only, no live runs from this authoring session, no commits): before
spending the real marketplace `deep-researcher` pack's expensive multi-session
run (`leg_d_deep_researcher.md`, driven separately by the orchestrator
against `external/clio-agent-marketplace/deep-researcher/` — **not** this
script's scope), rehearse the SAME two-server, multi-step shape that pack
relies on against a cheap, deterministic, purpose-built pack. This is the
STRESS GATE for legs B and D: it should go green before the real pack is run.

## What it proves

`agents/v2-stress/` declares BOTH the clio-kit `web` server (real network
fetch/search, task=required — the same mechanics `agents/web-testing/`
proves) AND the synthetic `v2ex` exerciser (`tests/test_tools/
mcp_exerciser.py` — the same mechanics `agents/v2ex-testing/` and
`agents/v2ex-avenues/` prove) on ONE `main` expert (six declared tools:
`web_fetch`, `web_search`, `v2ex_task_echo`, `v2ex_task_optional_echo`,
`v2ex_guarded_input`, `v2ex_staller` — within CLAUDE.md's RULE 5 5-7-tool
ceiling).

`leg_bd_stress.py` drives ONE session through ONE turn that forces, in a
single agent flow:

1. a `web_search` call;
2. a task-backed `web_fetch` of a small, stable public PDF
   (`https://www.unicode.org/versions/Unicode15.0.0/ch01.pdf` — Unicode 15.0 ch.1, version-pinned immutable path,
   picked small and hosted on the RFC Editor deliberately: about as
   stable/public a host as exists, and short enough to keep a live docling
   pdf→md conversion cheap);
3. a PLAIN HTML `web_fetch` of the SAME stable URL `leg_b_web_fetch.py`
   already proved live (`https://www.iana.org/help/example-domains`) as a
   contrasting second call in the SAME turn;
4. a task-backed `v2ex_task_echo` call carrying a nonce the agent must
   round-trip verbatim into its final answer;
5. the `v2ex_guarded_input` MRTR arm, answered HEADLESSLY mid-turn via the
   SAME questions route leg C's turn 2 uses
   (`POST /v1/sessions/{sid}/questions/{question_id}/answer`).

**Orchestrator note:** this authoring session cannot itself probe the
network or run a live session, so the PDF URL's live reachability is
unverified from here — confirm it resolves (or swap it) before relying on
requirement 2 in a real run. The HTML URL is already proven live by
`leg_b_web_fetch.py`.

## How to run

```bash
# Print the resolved prompt/nonce/needed-tools and exit. Boots nothing.
uv run python scripts/live_verification/leg_bd_stress.py --dry-run

# Boot the server, materialize+install+activate the v2-stress pack, assert
# BOTH servers ready at handshake and all six tools resolved on the agent.
# Zero LM spend.
uv run python scripts/live_verification/leg_bd_stress.py --plumbing-only

# Full run: everything above PLUS the one directed claude_code/sonnet turn.
uv run python scripts/live_verification/leg_bd_stress.py --provider claude_code --model sonnet
```

`--port` (default 17984), `--ws-dir`, `--out`, `--turn-timeout-s`,
`--question-wait-s` are all overridable — see `--help`.

## Verdict JSON

`out/live-verification/leg_bd_stress_verdict.json`. Carries
`handshake_rows`/`web_row`/`v2ex_row`, `readiness_gate`, `turn_status`,
`question_surfaced`/`question_mode`, a `requirements` dict (one bool per
numbered step above, plus `nonce_round_tripped_in_task_echo_result` /
`nonce_round_tripped_in_final_answer` / `web_fetch_pdf_docling_output_
plausible`), and `evidence` (the actual tool-call rows + the final answer
text). `pass` is true only when the turn reached a genuine terminal status
(`idle`/`completed`, never `timed_out`) AND every `requirements` value is
true.

`web_fetch_pdf_docling_output_plausible` is a HEURISTIC (result text length
> 200 chars) standing in for "docling actually converted the PDF, not just
an error stub" — per the deep-researcher runbook's own convention, hand-review
the actual markdown quality in `leg_bd_stress_messages.json` after a green
run; a passing heuristic is necessary, not sufficient.

## If red

Read `leg_bd_stress_verdict.json`'s `requirements` dict first — it names
exactly which of the five steps failed, plus `evidence` for the raw tool-call
rows. A failure here BEFORE the real `deep-researcher` run is more actionable
signal than a failure discovered only after that far more expensive,
multi-session run: this stress gate isolates whether the two-server,
multi-tool-call-in-one-turn SHAPE itself works before blaming the pack.
