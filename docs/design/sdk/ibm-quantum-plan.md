# IBM Quantum in CLIO: implementation plan

This plan makes IBM Quantum a connector in CLIO, built the way the ParaView and KiCad connectors are built. CLIO's agent writes Qiskit programs itself and runs them through the tools of Jaime's fork of the Qiskit MCP servers (`JaimeCernuda/ibm-quantum-mcp-servers`). The fork gains local simulation, exact and with real device noise, so everything works with no IBM account. It also gains MCP Apps views for the circuit, the circuit as it lands on a device, the results and the device itself. A plugin that follows the connector carries the skills, and the hybrid quantum-classical classifier from Jaime's learning plan is the showcase.

Hardware submission is built and tested against the code paths IBM's own SDK exposes, but it is qualified only once an IBM Quantum API key exists. Everything under Done holds without one.

The plan has eight workstreams. They are not ordered. The work is one goal that runs until every item under Done holds, and the human reviews the finished branches.

1. **The connector and its plugin.** An `ibm-quantum` entry with IBM's logo and two optional needs, and an `ibm-quantum` plugin that follows it.
2. **One server.** The fork serves its circuit, runtime and docs tools from one process.
3. **Local simulation.** Exact, ideal-shot and noisy runs on this machine, under the same tools and job model as hardware.
4. **MCP Apps in the fork.** Four views and the tools that open and feed them.
5. **The views.** Circuit, layout, results and backend, each with the operations the person can do from it.
6. **Feedback to the agent.** What the person selects reaches the agent, and a mark drawn on a view arrives with the gates and qubits under it.
7. **The skills and the classifier case.**
8. **Qualification.**

## Contract

This plan is the contract for the eight workstreams. When code and this plan disagree, change the code. If the plan turns out wrong, choose the fix that best keeps its intent, update this document in the same change, and record the decision in the ledger.

| Document | Governs |
| --- | --- |
| This plan | The IBM Quantum connector, the fork's server, simulation, views and tools, the plugin and its skills |
| `docs/design/sdk/ibm-quantum-ledger.md` in clio-agent (new) | This plan's decisions and acceptance evidence |
| `plans/mcp-apps-surfaces-plan.md` (project) | Connector entries (CN1 to CN12), tabs, app context (MA8), app tools (MA11 to MA14), reopening (MA18, MA19), the app component and its channels (MA20 to MA22). This plan uses them and changes none of them |
| `plans/kicad-plugin-plan.md` (project), KF2 | `describe_region`, the convention for a mark that lands on an app. This plan uses it and changes nothing in clio-agent or gact-tui |
| `plans/dashboards-and-live-sources.md` and `plans/taskable-run-code-plan.md` (project) | Background run_code, streams and dashboards, used by the classifier case as they are |
| MCP Apps specification 2026-01-26 and the draft in `modelcontextprotocol/ext-apps` | The wire protocol. The draft governs app tools |
| Qiskit 2.x, `qiskit-ibm-runtime` and `qiskit-aer` documentation | Primitives, PUBs, ISA circuits, fake backends and noise models |

**Base.** `JaimeCernuda/ibm-quantum-mcp-servers` `main` at `b763581`. clio-agent-marketplace `develop`. clio-agent and gact-tui at the heads of the branches that carry the work of `plans/mcp-apps-surfaces-plan.md` and KiCad's KF2. The session writes their names and commits in the ledger header before it starts.

Decisions use the prefix `QC` for the connector, `QS` for the server and simulation, `QV` for the views, `QF` for feedback, `QP` for the plugin, and `QQ` for qualification scenarios.

## Rules

The megachange rules and the seven rules of the MCP Apps plan carry over. Five more apply here.

1. **The programming is the agent's.** CLIO's own agent writes circuits and the classical code around them. No service whose own model writes the program is used. The Qiskit Code Assistant server stays out of the fork.
2. **IBM lives in the fork and the plugin.** No Qiskit or IBM name, format or tool name appears in Python or TypeScript in clio-agent or gact-tui. CLIO knows a connector entry, a plugin and a surface definition.
3. **The view has no execution logic.** Every operation a view offers is a call to one of the server's tools. If an operation has no tool, the fork gets the tool.
4. **Local first, hardware on purpose.** Every tool that runs a circuit works on a local backend with no account. A hardware run happens only when the agent names a real backend, and only after it has read the remaining Open Plan time.
5. **No paid usage by accident.** Nothing in the fork or the skills creates an instance, picks a paid plan or selects an instance other than the one in the connector. A run whose estimated time exceeds what remains is refused with a message saying so.

## Out of scope

Do not implement, stub or reserve fields for these:

- Other vendors and aggregators (Amazon Braket, qBraid, IonQ, Quantinuum, Azure Quantum). A later connector can take them.
- CUDA-Q and GPU simulation on clusters. That comes with clio-relay.
- The fork's AI transpiler server (`qiskit-ibm-transpiler-mcp-server`) and gym server. They stay in the fork and are not served by the connector.
- Training a model on hardware. Hardware is for inference and short sampling runs.
- A drag-and-drop circuit editor. The circuit view shows and selects; the agent edits.
- Sessions, batches and dynamic circuits on hardware beyond what a single primitive job needs.
- A separate local simulator connector.

## Current state

Read at the base.

**The fork.**

- A uv workspace with five servers on `fastmcp` 3.4.4 (locked; `>=3.2.0,<4` declared) and `mcp` 1.26.0: `qiskit-mcp-server`, `qiskit-ibm-runtime-mcp-server`, `qiskit-docs-mcp-server`, `qiskit-ibm-transpiler-mcp-server` and `qiskit-gym-mcp-server`, plus the `qiskit-mcp-servers` meta-package that installs four of them. Each server is its own process with its own entry point.
- `fastmcp` 3.4.4 has an `apps` extra (it pulls in `prefab-ui`) and registers `ui://` resources from its tool decorator. No server in the fork registers a `ui://` resource.
- `qiskit-mcp-server` (no account): `transpile_circuit_tool`, `analyze_circuit_tool`, `compare_optimization_levels_tool`, QASM 2 and 3 and QPY conversion, `load_circuit_from_qasm_tool`, `export_circuit_to_qasm_tool`, and resources for basis gates and topologies.
- `qiskit-ibm-runtime-mcp-server`: account setup and listing, `list_backends`, `least_busy_backend`, `get_backend_properties`, `get_backend_calibration`, `get_coupling_map`, `find_optimal_qubit_chains`, `find_optimal_qv_qubits`, `list_my_jobs`, `get_job_status`, `get_job_results`, `cancel_job`, `usage_info`, `run_sampler` (default 4096 shots) and `run_estimator`.
- Credentials come from `QISKIT_IBM_TOKEN`, then `~/.qiskit/qiskit-ibm.json`. `QISKIT_IBM_RUNTIME_MCP_INSTANCE` takes the instance CRN and avoids a slow lookup across instances.
- `run_sampler` and `run_estimator` always initialize a `QiskitRuntimeService` and resolve a real backend. Fake backends (`FakeProviderForBackendV2`, names with a `fake_` prefix) are used only by `get_coupling_map`. Nothing in the fork simulates locally, and no package depends on `qiskit-aer`.
- `run_estimator` takes one `parameter_values` list and builds one PUB with one parameter set. A batch of inputs in one job is not possible through the tool.
- `get_job_results` returns counts and is documented for Sampler jobs.

**CLIO.**

- `catalogs/connectors/*.yaml` takes a `command` server or a `url` server per entry, with `needs` of kind `address`, `sign-in`, `secret` or `value`. One server per entry.
- The MCP Apps host is in `src/clio_agent/gact/mcp_apps.py` and the files around it. The ParaView and SPICE entries are the models for a command server.
- `run_code(background=True)` runs a script as a task. The `live-status` skill in the `streaming` builtin plugin writes readings to a stream in clio-core with `stream.open(...)` and `s.write(...)`, wakes the conversation with `task.notify`, and a dashboard can bind to the stream.

**The classifier case.** Jaime's doc "Hybrid Quantum-Classical Classifier: Learning Plan": a small CNN reduces MNIST 0 and 1 to two features, a 2-qubit circuit (`zz_feature_map(2)` then `real_amplitudes(2, reps=1)`) wrapped by `EstimatorQNN` and `TorchConnector` replaces the last layer, trained with `StatevectorEstimator`. Inference on hardware binds 20 test images into one EstimatorV2 job with the observable `ZZ`. A `nn.Linear(2, 1)` baseline is the honest comparison. The companion doc "Getting Started with IBM Quantum (Open Plan)" covers the account, the instance CRN and the Bell-state first program.

## Design: the connector and its plugin

**QC1. The entry.** `catalogs/connectors/ibm-quantum.yaml`:

```yaml
- id: ibm-quantum
  name: IBM Quantum
  group: Quantum computing
  logo: ibm-quantum.svg
  description: Write and simulate quantum programs, see their circuits and results, and run them on IBM quantum computers
  server:
    command: uvx
    args: [--from, "git+https://github.com/JaimeCernuda/ibm-quantum-mcp-servers@<pinned commit>#subdirectory=ibm-quantum-mcp-server", ibm-quantum-mcp-server]
    env:
      QISKIT_IBM_TOKEN: ${need.token}
      QISKIT_IBM_RUNTIME_MCP_INSTANCE: ${need.instance}
  needs:
    - {key: token, kind: secret, label: IBM Quantum API key, required: false,
       help: Only for real hardware. Simulation needs no key.}
    - {key: instance, kind: value, label: Instance CRN, required: false,
       help: The Open Plan instance, starting with crn:v1. From the Instances page.}
  plugin: ibm-quantum
```

- With both needs empty, the connector is ready and every local tool works. An empty optional need leaves its variable unset.
- The server never reads `~/.qiskit/qiskit-ibm.json` when it runs under CLIO. The fork's credential lookup gains a switch (`QISKIT_MCP_NO_SAVED_ACCOUNT=1`, set by the entry) so the key comes only from CLIO's credential store (CN8).

**QC2. The logo** is IBM Quantum's mark from IBM's brand assets. The ledger records the source and terms (CN11). *Amended (QC2-D1):* IBM permits no use of its logos without written permission, so the logo is a qubit icon drawn for CLIO until the owner has IBM's permission.

**QC3. Asking for the key.** A hardware call without a key returns a typed result saying a key is needed and where to get it. The agent calls `offer_plugin(ibm-quantum, reason, needs=[token, instance])`, which raises the setup card with those two rows on a connector that is already on. The secret goes to the credential store, the server restarts (CN7) and the agent calls again. Not now is a typed refusal, and the agent goes on locally. This is the KiCad KC5 pattern and adds nothing to CLIO.

**QC4. The plugin.** `plugins/ibm-quantum` in clio-agent-marketplace:

- `plugin.json` with `extensions.ai.iowarp.clio`: `follows: ibm-quantum`, `requires: [visuals, science-log]` (`streaming` comes with `core`; ledger QC4-D1).
- *Amended (QC-D2):* `plugin.json`'s `connections: {"ibm-quantum": "ibm-quantum"}` binds the server name `ibm-quantum` to the connector's connection, as the base's connector plugins do; there is no `mcp.json`.
- `skills/` holds the skills in QP1.
- *Amended (QV9-D1):* `skills/quantum-workbench/references/quantum-workbench.json` holds the workbench surface (QV9), published by the agent with `create_a2ui_surface`; no loader reads an `ai.iowarp.clio/surfaces/` folder.

## Design: one server

**QS1. `ibm-quantum-mcp-server`.** A new workspace member that builds one FastMCP server and mounts `qiskit-mcp-server`, `qiskit-ibm-runtime-mcp-server` and `qiskit-docs-mcp-server` into it with FastMCP's server composition. It also holds the simulation (QS3 to QS7) and the views (QV1 to QV8). The three mounted servers keep working as standalone servers.

**QS2. Names.** Mounted tools keep their names without the `_tool` suffix the runtime server uses, and the duplicate `setup_ibm_quantum_account` from the transpiler server is not mounted. The ledger records the final tool list and the generated tools reference matches it.

## Design: local simulation

**QS3. Local backends.** `run_sampler` and `run_estimator` take three kinds of `backend_name`, and only the third needs an account:

| `backend_name` | Runs | Engine |
| --- | --- | --- |
| `local_exact` | Exact results, no shots for the Estimator | `StatevectorSampler` and `StatevectorEstimator` from Qiskit |
| `local_aer` | Ideal shots | `qiskit-aer` `AerSimulator` |
| `fake_<name>` | Shots with that device's noise, coupling map and basis gates | `AerSimulator.from_backend` on the fake backend from `FakeProviderForBackendV2` |
| any other name | The real device | `qiskit-ibm-runtime` primitives |

No `backend_name` means `local_aer`. Hardware is never the default (rule 4). `qiskit-aer` becomes a dependency of the new server.

**QS4. Noisy runs transpile like hardware.** A `fake_<name>` run is transpiled to that fake backend with the same preset pass manager and optimization level a hardware run would use, so its layout and gate counts match what the device would get. The result carries the transpiled circuit's QASM 3, its layout and its two-qubit gate count.

**QS5. One job model.** Every run, local or hardware, returns a `job_id`. Local jobs get ids with the prefix `local-`, run in the server's process off the event loop, and are kept in a job store under the server's state folder with their circuit, PUBs, backend, options and results. `get_job_status`, `get_job_results`, `list_my_jobs` and `cancel_job` treat local and hardware ids the same way. `list_my_jobs` shows local jobs first and marks the kind.

**QS6. PUBs, not one circuit.** `run_estimator` takes `parameter_values` as one set or a list of sets, and `observables` as one or a list, and builds the PUB the primitive expects. `run_sampler` takes a list of parameter sets the same way. The classifier's 20-image inference is one call and one job, locally and on hardware.

**QS7. Results for both primitives.** `get_job_results` returns counts and quasi-probabilities for Sampler jobs, and expectation values with standard errors per parameter set for Estimator jobs, with the job's backend kind. A new `compare_jobs(job_ids)` returns aligned results for jobs that ran the same circuit on different backends: count distributions with total variation distance, or expectation values with per-set differences.

**QS8. Hardware runs read the budget first.** Before a hardware job is submitted, the tool reads `usage_info`. If the job's estimate (from the runtime's own estimate when available, otherwise shots times circuits times the backend's reported per-shot time) exceeds the time left, the tool refuses with both numbers (rule 5). The result of every hardware job carries the seconds it used and the seconds left.

## Design: MCP Apps in the fork

**QV1. The Apps extension.** The new server installs `fastmcp[apps]` and registers its views as `ui://` resources from the tool decorator. View tools return a useful text result to a host without apps.

**QV2. One bundle, four views.** A package in the fork, `packages/quantum-views`, builds one self-contained HTML file with Vite: TypeScript, `@modelcontextprotocol/ext-apps`, and Vega-Lite vendored for charts. Circuits and device maps are drawn by the bundle's own SVG code from JSON the server sends (QV5). No circuit drawing library is used. The server registers the bundle four times:

`ui://ibm-quantum/circuit`, `ui://ibm-quantum/layout`, `ui://ibm-quantum/results`, `ui://ibm-quantum/backend`.

Each resource declares an empty CSP. The page loads nothing from the network.

**QV3. The opening tools.** Read-only, each bound to its view:

| Tool | Opens | Arguments |
| --- | --- | --- |
| `view_circuit` | `ui://ibm-quantum/circuit` | `circuit` (QASM 3 or QPY) or `job_id`, optional `blocks` (named gate ranges such as `feature map` and `ansatz`) |
| `view_layout` | `ui://ibm-quantum/layout` | `circuit` and `backend_name`, or `job_id`; optional `optimization_level` |
| `view_results` | `ui://ibm-quantum/results` | `job_ids` (one or more), optional `labels` per job, optional `expected` (true labels for a classifier batch) |
| `view_backend` | `ui://ibm-quantum/backend` | `backend_name` (real or `fake_<name>`) |

Every view opens from a `job_id`, so a job submitted from run_code with the SDK directly opens the same way once its id is known. For a hardware job the server reads it through the runtime. For a run_code job on a local simulator, the agent passes the circuit and its counts or values to `record_job`, which puts them in the local store under a `local-` id.

The text result tells the model what was opened and a short summary: qubits, depth, two-qubit gates, shots, the best and worst parameter sets, the accuracy against `expected`.

**QV4. Data reaches the view through app-only tools.** Visible to the app and not to the model:

- `view_data_circuit`: the circuit as a JSON list of qubits, clbits and gate layers with parameters, and its blocks.
- `view_data_layout`: the logical circuit, the transpiled circuit, the virtual-to-physical map, the inserted swaps, and the device's coupling map with positions.
- `view_data_results`: the jobs' aligned results from `compare_jobs`.
- `view_data_backend`: the coupling map with positions, per-qubit T1, T2 and readout error, per-edge two-qubit error, the queue length and, for a real device with a key, the Open Plan time left.

**QV5. Device positions.** The server computes 2D positions for a coupling map once per backend with a deterministic graph layout and keeps them in its state folder, so heavy-hex devices look the same every time. Positions are part of `view_data_layout` and `view_data_backend`.

**QV6. Reported context.** Each view reports through `ui/update-model-context`, at most ten times a second per key (MA22):

- `selection`: the kind (`gate`, `qubit`, `physical_qubit`, `edge`, `bitstring`, `parameter_set`), its index or name, and for a gate its layer, qubits and parameters.
- `job`: the job ids the view shows.

**QV7. App tools for the agent.** Registered by the views under the draft's app tools (MA11 to MA14):

| App tool | View | What it does |
| --- | --- | --- |
| `focus` | circuit, layout | Highlights gates, a block or qubits |
| `show_jobs` | results | Sets which jobs are compared |
| `capture` | every view | Returns a PNG of what the view shows now |
| `describe_region` | circuit, layout, backend | See QF2 |

**QV8. Reopening.** The opening call carries its arguments and the local job store keeps local results, so a replay (MA19) restores every view, local or hardware.

**QV9. The workbench.** One A2UI surface, `quantum-workbench`, places the circuit, layout and results views together and links them by selection: a gate selected in the circuit view is highlighted on the physical qubits in the layout view.

## Design: the views

Each operation below is one call through `callServerTool` to the tool named (rule 3).

**QV10. Circuit.** Wires, gates by layer, parameters on hover, named blocks shaded. Operations: show as QASM 3 (`export_circuit_to_qasm`), analyze (`analyze_circuit`).

**QV11. Layout.** Two panes: the logical circuit, and the device map with the used physical qubits and the swaps drawn on its edges. A slider steps through the transpiled circuit layer by layer on the map. The header shows depth and two-qubit gates before and after. Operations: re-transpile at another optimization level (`transpile_circuit`), compare levels (`compare_optimization_levels`), pick another backend from a list (`list_backends`, with fake backends always listed).

**QV12. Results.** For Sampler jobs, grouped bars per bitstring with one series per job, and the total variation distance between them. For Estimator jobs, one point per parameter set with one series per job, the expected label when given, and accuracy per job. Operations: run this circuit on another backend (`run_sampler` or `run_estimator` with the job's PUBs, the backend picked from a list), add a job to the comparison.

**QV13. Backend.** The device map colored by a chosen metric (T1, T2, readout error, two-qubit error), the queue length and, with a key, the time left. Operations: switch metric, find good qubit chains (`find_optimal_qubit_chains`), open a layout for a circuit on these qubits.

## Design: feedback to the agent

**QF1. Selection is context.** "Why is there a swap here?" works because the layout view reported the selected edge and layer (QV6).

**QF2. A mark on an app.** The views register `describe_region` (KiCad KF2's convention). The circuit view returns the gates and qubits under the region with their layers. The layout view returns the physical qubits, edges and swaps. The backend view returns the qubits and edges with their metrics. Nothing changes in clio-agent or gact-tui.

## Design: the skills and the classifier case

**QP1. Skills in the plugin.**

- `qiskit-programming`: Qiskit 2.x as the session's version has it. Build circuits, transpile to an ISA circuit for the target, run with SamplerV2 or EstimatorV2 and PUBs, read results. Simulate with `local_exact` and `local_aer`, then `fake_<name>`, then hardware. Open `view_circuit` and `view_layout` when a circuit is first built for a device, and `view_results` after every comparison.
- `ibm-quantum-budget`: what the Open Plan gives, that only execution time counts, reading `usage_info` before any hardware run, keeping shots around 1000, one job per batch of inputs, never creating or switching instances.
- `hybrid-qml`: a quantum layer inside a PyTorch model with `EstimatorQNN` and `TorchConnector`. Train on `StatevectorEstimator` in a background run_code task that writes loss and accuracy per epoch to a stream. Evaluate the trained model on `local_exact`, then noisy on `fake_<name>`, then on hardware, each as one batched Estimator job. Always train the classical baseline beside it and report both, with the doc's caveats on what accuracy on MNIST 0 and 1 does and does not show.

**QP2. The classifier case.** One prompt runs the whole case from `hybrid-qml`:

1. The agent writes the hybrid model and the baseline, and starts training as `run_code(background=True)`. Each epoch writes `{epoch, loss, train_acc, model}` for both models to a stream `qml-training`. A dashboard bound to the stream shows both curves live.
2. When training ends, `task.notify` wakes the conversation. The agent opens `view_circuit` with the feature map and ansatz as blocks.
3. The agent computes the 20 test images' features in run_code and calls `run_estimator` once on `local_exact` and once on a Heron-class `fake_<name>`, with the trained weights and the features as 20 parameter sets.
4. The agent opens `view_layout` for the noisy job and `view_results` with both jobs and the true labels. The person sees exact against noisy expectation values per image, accuracy for each, and the baseline's accuracy in the text.
5. With a key, step 3 gains a third call on the least busy real device, after the budget check, and step 4 shows three series.

The science log records the comparison as a study, with each job id as an entry's source.

## Defaults for the open questions

- **Which fake backend.** The session picks the Heron-class fake backend in `FakeProviderForBackendV2` closest to the devices on the Open Plan, records it in the ledger, and the skills name it.
- **Job store size.** Local jobs are kept until the person deletes the server's state folder. No pruning.
- **QPY or QASM 3 in views.** The views take both and send QASM 3 to the model.

## What the session installs

- The fork's workspace with `uv sync`, plus `qiskit-aer`, `fastmcp[apps]`, `torch` and `torchvision` (CPU) for the classifier case.
- Node and Vite for `packages/quantum-views`.
- MNIST through `torchvision`, cached in `.cloud-run/`.

## What the ledger records

- The base refs, the machine, the chosen fake backend and the final tool list.
- Every decision with its evidence, and every amendment to this plan.
- The logo's source and terms.
- For the classifier case: the accuracies of the hybrid model and the baseline on `local_exact` and the fake backend, the job ids, and the screenshots read.

## Qualification

Run focused tests in the fork and `ruff` on touched paths while working, plus the fork's own checks. Then run these against a freshly restarted stack (`claude_code` provider), driving the real UI, and record each in the ledger with commands, handles and the screenshots read. UI checks run at 1440 by 900 and 390 by 844, with `consoleErrors` and `failedRequests` empty.

**The connector and simulation.**

| ID | Scenario | Passes when |
| --- | --- | --- |
| QQ1 | Open Extensions | IBM Quantum is listed with its logo. Its card shows two optional needs and none is required |
| QQ2 | Enable it with no key. Ask for a Bell state, simulated | `local_aer` runs it. Counts are only `00` and `11`, and the result has a `local-` job id |
| QQ3 | Run the same circuit on the fake backend | Counts include `01` and `10`. The result carries the transpiled QASM 3, its layout and its two-qubit gates |
| QQ4 | Ask to run it on a real device, with no key | The typed result says a key is needed. The setup card shows the two rows. Not now leaves the agent working locally |
| QQ5 | A home folder with a saved `~/.qiskit/qiskit-ibm.json` and no key in CLIO | The server does not read it and reports no key |
| QQ6 | Run a 20-set Estimator batch on `local_exact` | One call, one job, 20 expectation values with standard errors |
| QQ7 | Restart the server, list jobs | Earlier local jobs are listed with their results |

**The views.**

| ID | Scenario | Passes when |
| --- | --- | --- |
| QQ8 | `view_circuit` on a GHZ circuit, then select a gate | The gate is in the next turn's context with its layer and qubits |
| QQ9 | `view_layout` on a 5-qubit circuit for the fake backend, step through layers | Swaps are drawn on device edges. Header counts match `analyze_circuit` |
| QQ10 | From the layout view, re-transpile at level 3 | The view updates through `transpile_circuit` and the two-qubit count changes or the ledger says why not |
| QQ11 | `view_results` with the QQ2 and QQ3 jobs | Grouped bars and the total variation distance match `compare_jobs` |
| QQ12 | Draw a mark over two gates in the circuit view and ask about them | The mark's context lists those gates from `describe_region` |
| QQ13 | `view_backend` on the fake backend, color by two-qubit error | Edges are colored and the legend matches `get_backend_calibration` |
| QQ14 | Reopen a closed session with these views | Every view comes back with its data |
| QQ15 | A job submitted from run_code with the SDK on Aer, recorded with `record_job` | `view_results` opens it and compares it with a server job |

**The classifier case.**

| ID | Scenario | Passes when |
| --- | --- | --- |
| QQ16 | One prompt: "Build a hybrid quantum-classical classifier for MNIST 0 vs 1, compare it with a classical baseline, and show me how hardware noise changes it" | QP2 steps 1 to 4 happen without further prompting |
| QQ17 | Watch training | The dashboard shows both models' curves updating per epoch from the stream |
| QQ18 | After training | `view_results` shows exact and noisy expectation values for 20 images with accuracies, and the baseline accuracy is in the reply |
| QQ19 | Ask why one image flipped class under noise | The agent answers from the selected parameter set and the layout, not from general text |

**Hardware phase.** Run only once an IBM Quantum API key and Open Plan CRN are in the connector. Rows are marked hardware-phase in the ledger.

| ID | Scenario | Passes when |
| --- | --- | --- |
| QQ20 | Add the key and CRN, ask for the time left | `usage_info` answers. The key is in no model text, transcript, record or log |
| QQ21 | Bell state on the least busy device, 1000 shots | The budget check ran first. The job's result carries seconds used and left. A background task watched the queue and `task.notify` woke the conversation |
| QQ22 | QP2 step 5 | `view_results` shows exact, noisy and hardware series for the 20 images |
| QQ23 | Ask for a run larger than the time left | Refused with both numbers. Nothing is submitted |

## Done

1. Every decision is implemented. `run_sampler` and `run_estimator` run on local backends with no account, and the mounted servers still run standalone.
2. The fork's checks pass: tests and the generated tools reference. `ruff check` and `ruff format --check` are clean on touched paths in every repository.
3. Every scenario from QQ1 to QQ19 passes against a freshly restarted stack, each shown with commands, handles and the screenshots read.
4. The hardware path is implemented, unit-tested against `qiskit-ibm-runtime`'s own test doubles, and QQ20 to QQ23 are written in the ledger ready to run.
5. The ledger holds every decision with its evidence and every fact listed above.
6. The site's user guide has a page on quantum programs with IBM Quantum in CLIO, including the classifier case's export.
7. The branches are pushed in the fork, clio-agent and clio-agent-marketplace. The entry pins the fork's pushed commit. The draft PRs carry a summary and the evidence.

## Working conventions

- Use the branch the cloud session is set up to push to, with the same name in every repository, and write it in the ledger header. Open draft PRs against `develop` in the IOWarp repositories and against `main` in the fork.
- Small Conventional Commits, with test evidence in each PR description.
- Keep runtime state in `.cloud-run/`. Stop the stack and every background task when you finish a work block.
