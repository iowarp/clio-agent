---
name: grind-clio-case
description: >-
  Build and grind a CLIO benchmark "case" to reliable acceptance — find a hard,
  data-grounded NDP question with a vivid output, author the Agent Blueprint,
  wire the agent-test harness, then iterate live runs by reading traces until it
  passes ≥0.8 across distinct scenarios. Runs a SECOND, parallel CLIO instance so
  it never disturbs a grind already in flight. Use when asked to start, build, or
  grind a new CLIO case, or to continue a case's GOAL.md grind loop.
---

# Grind a CLIO case

This is the methodology for taking a CLIO benchmark case from an idea to a
reliably-passing, honestly-grounded acceptance test. It is the same work the
EarthScope (case02) and Wildfire grinds did — distilled so it can be repeated
on a new NDP case **in parallel** with a grind that is already running.

You are doing two jobs at once. Internalize the division of labor first, then
the non-negotiables, then the mechanics.

---

## The two monitors (division of labor)

A case is judged by **two** instruments, and you own one of them:

- **agent-test (the data-pathway monitor).** Jaime's `~/agent-test` pytest
  plugin (github.com/JaimeCernuda/agent-test). Its matchers assert on the
  *normalized trace*: which tools were called, the route taken, structured
  workflow state, artifacts on disk. This is the mechanical green/red grid.
- **You (the semantic monitor).** You read the raw trace in `runs/` and judge
  *meaning*: did the agent reason about the right thing, select for the right
  reason, explain the right conclusion? **The green grid never substitutes for
  the trace read.** Every distinct failure you find by reading a trace becomes a
  new matcher — that is how the test suite earns its strength.

It is a combined job: agent-test catches pathway regressions fast; you catch
semantic emptiness the matchers can't see; and you iterate to improve *both* —
the agent's prompts/tools **and** the matchers.

---

## Non-negotiables (the hard lessons — do not regress on these)

These are not style preferences. Every one was learned by violating it and
paying for it. They override convenience and they override the green metric.

1. **No gates, no fake data, no `src/` mocks.** A passing JSONL counter or a
   single happy-path unit test is not a pass. Acceptance = a real live session
   whose trace, per-branch evidence, artifact, and provenance you inspect and
   confirm match the prompt's intent.

2. **Decisions live in the agent's reasoning. Code only runs tools and records
   their data.** This is *the* trap. A decision hardcoded in Python is the
   deterministic trap whether it is a `when_child_completed` contract, a
   `max(acres × uncontained)` heuristic, or `impact = matched_count > 0`. They
   are the same sin. The runtime may **persist** data and **compute geometry via
   tools** and **record tool outputs** (a bbox a tool returned, an overlap
   count, which files were saved). It must never **choose** which thing matters,
   which region to analyze, or whether the answer is "impact" vs "null". When you
   catch yourself making the agent reliable by moving its judgment into runtime
   code, stop — you are building a hardcoded pipeline wearing an agent costume.

3. **Routing is DSPy-typed, not string-matching.** Expert handoffs come from
   typed structured workflow state / `when_state` on *data the tools produced* —
   never `when_request_contains: "<city>"`, never a forced mandatory branch, and
   never an env var that force-feeds the answer. The case must generalize across
   regions/scenarios with no benchmark-specific string contracts.

4. **Difficulty is intrinsic.** The hard part of the question must be the
   *reasoning*, not a parsing trick. Selection is by meaning (e.g. downwind
   *impact*, not perimeter *size*). The "boring" outcome (no impact, contained,
   nothing found) is a **correct, reachable answer** — not a failure to paper
   over.

5. **Don't trust green — read the trace.** A test can pass on a semantically
   empty run (literal `{{workflow_state.region}}` placeholders written into tool
   args; a null answer accepted by a too-loose matcher). The moment a run goes
   green, open its trace and confirm it earned it. False passes are the default
   failure mode, not the exception.

6. **Never write a green completion summary over an unmet metric.** If a Done
   criterion isn't met, say so plainly with the number. Don't reach for
   off-ramps ("honest status", "stopping here") to escape a grind that isn't
   finished. The grinds that work run 25+ hours and 80+ iterations — a few hours
   in is a third of the way, not the end.

7. **Topology: domain-grouped, re-entrant — not a linear chain.** A linear
   `a → b → c → d` chain forces deterministic semantics and is where the
   forced-handoff pain lives. Group sub-experts by domain under `main`, and have
   `main` *re-decide* after each domain returns, routing on typed workflow state
   (EarthScope's re-entrant `main ↔ data ↔ analysis` pattern). Selection logic
   lives in an expert's reasoning over typed state, never as a routing string.

8. **clio-agent core is composable infrastructure — building a case never edits
   it.** This is the whole point: clio-agent is a *composable agent* you build
   cases *on*, not source you modify per case. The only tools hardcoded into
   core are **universal defaults every case needs** (filesystem, bash, web).
   Every domain/case-specific tool is an **MCP server**, which clio-agent
   *installs / mounts* through a **generic MCP mechanism** — the same mechanism
   for *any* MCP, ours or third-party; nothing is special-cased and there is
   **no per-domain code in core at all** (no bridge file, no shim). `clio-kit` is
   not a privileged integration — it is simply where *we* publish our own MCPs
   for community sharing; it gets no extra support or tests, it's just another MCP
   collection. (The in-core servers `geospatial_server.py`, `sac_server.py`,
   `ndp_server.py`, etc. are the *legacy* pattern being migrated out; they are
   what's being cleaned up, not a model to copy.)
   And when a case reveals a missing *class* of capability (e.g. this case needs
   a "monitor"), you do **not** hardcode that case's version — you implement the
   generic ability to *install* that capability on clio-agent for **all** cases
   and expose its semantics to future cases. Running many cases is exactly how
   those generic capabilities get discovered and promoted into reusable
   infrastructure. **If you are editing `src/clio_agent/` to build a case, stop**
   — that belongs in an MCP (installed generically; publish in clio-kit if it
   should be shared) or in a generic installable mechanism (infrastructure),
   never as a case-specific built-in.

---

## Running a SECOND CLIO in parallel (don't disturb a grind in flight)

A grind may already be running (its own server, ARC store, traces, and git
branch). To work a new case without colliding, isolate **five** things: git
worktree, port, data dir, allowed roots, and artifacts dir. See
`parallel-clio.md` for the full rationale; the essentials:

```bash
# 0. Find the running instance's port so you avoid it (pidfile + listeners).
cat ~/.local/share/clio/clio-server.pid 2>/dev/null
ss -ltnp 2>/dev/null | grep -E '178|179|81|80' || true   # known CLIO ports

# 1. Isolate the CODE: a git worktree on a fresh branch (don't share the
#    other grind's branch / working tree).
cd /home/jcernuda/clio-agent
git worktree add ../clio-<case> -b feat/<case>
cd ../clio-<case>

# 2. Isolate STATE + FILE SANDBOX + ARTIFACTS + PORT. Pick a port nobody uses.
export CASE2_PORT=17970                                   # not 17800/17960/8100/8000
export CLIO_DATA_DIR="$PWD/.clio_agent"                   # separate ARC store
export CLIO_ALLOWED_ROOTS="$PWD:/tmp"                     # separate file sandbox
export CLIO_ARTIFACTS_ROOT="$PWD/.clio/artifacts/geo"     # separate geo renders

# 3. Start an isolated gact server (foreground in its own shell, or backgrounded).
uv run uvicorn clio_agent.gact.app:app --host 127.0.0.1 --port "$CASE2_PORT"

# 4. Point the agent-test harness at YOUR server, enable live, pick the provider.
export CLIO_GACT_URL="http://127.0.0.1:$CASE2_PORT"
export CLIO_RUN_LIVE=1
uv run pytest tests/test_real_cases/test_<case>.py --provider argonne_metis -m "real_case and live"
```

Isolation rules:
- **Install the new blueprint at `scope: workspace`** (lands in `$PWD/.clio/agent-blueprints`), not global — so you never touch the other grind's pack or the global registry.
- Two grinds hitting **live ALCF** concurrently is fine (Globus token is shared via keyring); just mind rate limits.
- Singletons to respect: the launcher pidfile (`$CLIO_PREFIX/clio-server.pid`) and the port. There are no core lock files — isolation is by port + data dir.

---

## The case lifecycle

Not a schedule — a dependency order. Do the next incomplete thing; one complete,
verified unit at a time.

**Discover a worthy question.** Browse the live NDP catalog for a question that
is (a) genuinely hard, (b) answerable *only* by grounding in real data, and (c)
produces a vivid / visual / "wow" output. The output strength matters — a
correct answer with a weak visual is a weak case (this is exactly why the fire
case was preferred over EarthScope's flat CSV output). Confirm the difficulty is
intrinsic and selection is by meaning, not size/parsing.

Pick the science FIRST, then build the system around it — never survey the
existing tools and reverse-engineer a question to fit them. Three hard gates,
all checked *before* you commit:
- **Fresh domain.** Distinct from every existing case in *both* tooling and
  *domain*. An adjacent domain (e.g. earthquake seismology next to EarthScope
  GNSS geodesy) is weak even if the data/method differ — for a multi-case
  portfolio, domain spread is itself a goal.
- **On NDP, verified live.** The dataset must be discoverable through the NDP
  catalog (`ndp_search_datasets`) and reachable through the NDP MCP. Verify the
  exact resource is live *now* (query it) — NDP entries go stale (a decommissioned
  endpoint looks fine in the catalog and 404s on use). A question whose data is
  not on NDP is not an NDP case, no matter how good the science.
- **Buildable in the budget.** Grindability (bulletproof, reachable data) is a
  legitimate tie-breaker between equally-fresh, equally-grounded options — not a
  reason to pick a stale domain or off-NDP data.

**Build only the novel tool — as an MCP, never in clio-agent core.**
Separate three layers and add only the vertical: *retrieval* reuses an existing
retrieval MCP (e.g. the `ndp` MCP — search/stage/query-arcgis), driven by a good
expert prompt; *visualization* reuses an existing plot MCP (grow a *generic*
plotter if a chart type is missing); the **domain-specific analysis** is the only
thing that justifies a new server — and that server is a **new MCP**, installed
into clio-agent through the generic MCP mechanism (no per-domain code in core — no
bridge file, no shim). Publish it in `clio-kit` if it should be shared, but that's
just our MCP collection, not a special home. Never add a case-specific server
under `src/clio_agent/tools/servers/` (that is core infrastructure — see
non-negotiable #8). A per-case retrieval fetcher or
per-case plotter, or a domain server baked into the agent core, all duplicate or
pollute horizontals the platform already has and don't compound.

**Pin the case down.** Create `benchmark/<case>/` with:
- `prompt.txt` — the natural-language task. Names **no** expert, tool, region
  string, or schema. It must read like a scientist asking a question.
- `README.md` — the question, the datasets, and a hand-built manual solution
  (`manual-solution/`, `manual-solution/DATASETS.md`) proving it's answerable.
- `GOAL.md` — the grind contract (copy `GOAL_TEMPLATE.md` from this skill and
  fill it). This is what the `/goal` loop reads each iteration.

**Author the Agent Blueprint** (marketplace pack). Domain-grouped topology
(non-negotiable #7). Root `AGENT.md` (YAML frontmatter: `id`, `root_expert`,
`experts: [...]`) + `experts/*.md`, each with a real 500+ word domain prompt, a
typed signature, 5–7 curated tools, `structured_outputs: { workflow_state: true,
... }`, and `continuation_contracts` that route on `when_state` over typed data.
Install workspace-scoped for the grind.

**Wire the harness.** Add `tests/test_real_cases/test_<case>.py` mirroring
`test_earthscope_case.py`: `@pytest.mark.real_case`, `@pytest.mark.live`, drive
the `agent` fixture with `agent.run({task, blueprint_id, case_dir, run_label,
timeout_s})`. Matchers read **structured** evidence (tool outputs, route, state,
a non-empty artifact on disk) — never synthesis prose. Each matcher must be
proven offline to FAIL a tampered/bad run before you trust it.

**Validate wiring fast, then run live once.** Confirm the SUT can set
provider/model, activate the blueprint, run a turn, and normalize the trace.
Then do ONE live run on the guardrail cell, capture to `runs/`, and read it by
hand. Single cell only — defer agent-test's model-matrix and prompt-search until
the case is productive (that's the payoff phase, not the setup phase).

---

## The grind loop

This is what `/goal` runs each iteration. Keep it tight:

1. Read `GOAL.md` Status + priority order. Take the next incomplete step.
2. Do ONE complete unit of work.
3. **Run live → read the trace in `runs/`** (don't trust green). Judge semantics.
4. Whatever the trace read discovers becomes a fix: an agent prompt change, a
   tool change, or a new matcher/test. Fix the real thing, not the metric.
5. Verify per the non-negotiables; confirm no regression (the other case's tests
   must still pass — e.g. EarthScope's blueprint suite).
6. Commit on the correct branch with a `docs(grind):`/`fix(runtime):` message
   recording the iteration number and the measured rate (e.g.
   `r12 SW 9/10=0.90`).
7. Update `GOAL.md`'s Status section. Repeat.

Stop the loop and ask Jaime only on: a genuine design decision, an external
blocker (ALCF auth, flaky NDP), or when all Done criteria are met. Don't barrel
past shortcuts; don't quit early over a not-yet-met metric.

---

## Done criteria (hard — ALL must hold)

A case is a grind, not a one-shot. The canonical bar (tune per case in GOAL.md):

1. **Reliability:** the case test passes live at **≥0.8 over ≥10 sampled runs**
   on the guardrail cell (`argonne_metis`/`gpt-oss-120b`); report the Wilson
   interval, lower bound ≥0.6. One green run is not done.
2. **Generalization:** the same bar holds for **≥3 distinct scenarios**
   (regions/conditions) via mutated prompts — proving no string dependence.
   Traces for all in `runs/`.
3. **Honest negative path:** at least one scenario whose correct answer is the
   "boring" one (no impact / nothing found) returns it correctly (no forced
   artifact), asserted by a dedicated test, also ≥0.8 over ≥10.
4. **Tamper-proof matchers:** every matcher reads structured evidence and is
   proven offline to FAIL a tampered run.
5. **The suite grew from review:** every distinct failure you found reading a
   trace is encoded as a matcher / unit test.
6. **No regressions:** the other cases' suites still pass; your changes are
   additive and scoped.

---

## Anti-pattern catalog (failure modes that actually happened)

- **Env var force-feeding the answer** (`CLIO_WILDFIRE_REGION_BBOX`): the agent
  rode along while the runtime/test fed it the region. "3 regions ≥0.8" was fake.
  → Region comes from the agent reasoning over the prompt.
- **Runtime selection heuristic** (`max(acres × uncontained)` in
  `_infer_workflow_state_from_tool_call_row`): the intellectual core — *which
  thing matters* — moved out of the agent. → Selection belongs to the expert.
- **Runtime decision** (`impact.present = count > 0`): the analysis expert's job
  hollowed out. → The tool gives the number; the agent makes the call.
- **Retry contract that regressed** (retry an expert when state ungrounded):
  broke the routing chain (6/10 → 0/3). → Don't bolt reliability hacks onto the
  loop; fix the underlying expert/tool. When a change regresses, revert to the
  last known-good baseline before iterating further.
- **False pass on placeholders**: model wrote literal `{{workflow_state.region}}`
  into tool args; region was `None`; null answer accepted. → Matchers must reject
  `None` region / `{{` template literals / unevaluated overlap.
- **Off-ramping** ("honest status, stopping here") a third of the way into a
  grind. → If a Done criterion is unmet, the grind isn't done; keep going.
- **Tools-first / adjacent-domain case pick**: choosing a question to fit the
  existing toolbox (weak), then "correcting" into a domain next to an existing
  case (also weak). → Science first; fresh domain; build what's needed.
- **Off-NDP data**: a great question whose data isn't on the NDP catalog — the
  agent can't discover it and you end up with a curated direct-to-source fetcher
  (e.g. `seismic_query_catalog` calling `earthquake.usgs.gov` directly). → Verify
  NDP-listing live during discovery; if it's not on NDP, it's not an NDP case.
- **Per-case retrieval/plot tools**: a new domain server that re-implements
  fetching and plotting. → Only the domain *analysis* is novel; reuse the NDP MCP
  and the plot tooling for the horizontals.
- **Editing clio-agent core to build a case** (a new `src/clio_agent/tools/servers/<domain>_server.py`,
  gateway/catalog edits): pollutes the composable core with case-specific code.
  → Domain tools are **MCPs installed generically**; core holds only universal
  defaults (fs/bash/web). Case dev composes on the agent, it does not modify it.
  The case13 seismic server was built this wrong way and had to be relocated.
- **Hardcoding a one-off where a generic mechanism belongs**: a case needs a
  monitor / a new capability class, so you wire that one case's version. → Build
  the generic, installable capability for *all* cases and expose its semantics to
  future ones; that is how cases promote into infrastructure.

When in doubt, re-read non-negotiable #2. Almost every shortcut is a disguised
version of moving a decision out of the agent and into code.
