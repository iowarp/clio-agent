# Beta 3: October 5 Delta qualification

Publication is a separate decision. This procedure is prepared; it is **not a
record of fresh inference, a live SPOTTER verdict, or completed Delta acceptance**.
Use the commits of the reviewed beta-3 PRs, not an installer that still selects
beta 2. The branch and historical check inventory is in
[the integration status](../../design/beta3-integration-status.md).

## Prerequisites and identities

- The owner supplies the Delta allocation, compute-node name, SSH route and permitted
  persistent storage location. Allocation stays external to CLIO. Check its expiry
  before downloads and inference; record job/node identities in the evidence receipt.
- Google and Globus CLIO applications do not exist yet. Complete
  [registration](oauth-registration.md) before claiming browser sign-in qualification.
  An explicit desktop-folder transfer or SFTP materialization can prepare the OPAL
  data meanwhile; it does not count as a Drive/Globus acceptance pass.
- Use a model revision that fits the actual GPU and the pinned attention profile.
  Record the model repository, exact commit, available GPU memory and download bytes.
  A package install or successful `/health` response does not establish attention.
- Keep operator answer files outside every tested session's workspace, allowed roots
  and mounted data. Use neutral A/B session names; no evaluator answers or condition
  labels go into either parent or reviewer prompts.

## 1. Prepare the connected node

1. On the allocated node, inspect GPU/driver, runtime availability, storage capacity
   and write access. In Desktop, select the SSH route through the login host and
   identify the allocated node explicitly. Do not deploy inference on the login node.
2. Check out the exact Agent integration commit and its pinned UI/Marketplace
   submodules. Resolve the committed lock, including the exact Schemas revision.
   Keep authentication for private dependencies in the host credential mechanism.
3. Start that checkout's Agent on loopback. For an already-prepared source checkout:

   ```sh
   export CLIO_AGENT_HOME=/absolute/owned/path/clio-agent
   uv sync --frozen --extra dev
   uv run --frozen clio-agent serve --host 127.0.0.1 --port 8100
   ```

   Choose the owned persistent path after inspection; the example is a placeholder,
   not a Delta filesystem claim. CLIO state follows `CLIO_AGENT_HOME`, not the launch
   directory. Keep the server process and SSH tunnel alive throughout qualification.
4. Adopt/connect this existing Agent in Desktop, register an existing workspace on
   that node, and confirm the connected CLIO and execution host labels. Record the
   Agent SHA, UI SHA, Marketplace SHA, Schemas SHA, version, node and workspace IDs.
5. In **Models & storage**, choose the host, then set and probe model, service and
   evidence folders. On homelab use the owned `/data/clio-beta3-qualification` tree;
   the nearly full root disk is not a suitable model/image destination.

## 2. Connect and verify OPAL data

1. Use the source picker from Files or the composer. Select Google Drive, Globus,
   SFTP or an explicit desktop-folder transfer. Confirm which host receives the data.
2. Use **Read only** initially. Browse the selected root and materialize/transfer it.
   Record provider/source ID, source revision and terminal transfer-operation ID.
   Confirm the export's manifest and folder structure on the connected CLIO.
3. On disposable test data, separately check Working copy review, upstream-change
   conflict refusal and selective apply. Only offer Write enabled when supported;
   it must affect the actual selected folder. Keep upstream deletion separate.
4. In Marketplace, select APPL-CORE and let CLIO materialize its pinned blueprint.
   Confirm connected-host identity, effective revision and ready tools. Its dataset
   card helper now uses `CLIO_AGENT_WORKSPACE_STATE_DIR`, supplied by CLIO's shell.
   Inspect the returned dataset directory; no new workspace `.clio` is acceptable.

## 3. Prepare services and attention

1. Deploy **Flowcept** on the selected execution host. Pick the inspected dependency
   runtime and an owned service-data folder. For Podman, **Service folder** also
   keeps images/downloads on that volume; Engine storage keeps the existing engine
   store. Start it, run Verify, and retain its fresh provenance write/readback receipt.
   The collector is the sole Flowcept persistence owner.
2. Deploy **HPE CMF** independently. Start and verify its direct-server mode; the
   receipt must include the fresh execution and both input/output artifact edges.
   CMF-only operation must not depend on vLLM or Flowcept being running.
3. Download the chosen model at its exact repository revision. Inspect the destination
   and capacity, then complete/reuse the download before installing a runtime.
4. Deploy **Native CUDA + attention**, profile `vllm-0.27.0-attention-1`, using the
   downloaded model and the selected Flowcept settings file on that same host.
   This pins connector `95ab2acd6fe1be74ad9a3fa2aca1ecbee60a6284` and Flowcept
   `e638b4e2072290a2921965a03a150db124e11c2e`. The launcher activates the probe before
   constructing the engine. Plain native vLLM serving is a different profile.
5. Start the runtime; inspect effective configuration and logs. Connect/use the
   endpoint in the session separately. Select the model explicitly, not through a
   mutable global default. Configure provenance queries and the local attention
   evidence folder; confirm SPOTTER uses the same profile/reducer and readable files.
   The attention reader must not fetch files over SSH.
6. Run one fresh inference together. Verify the capture's digest, shape, actual
   tokenizer mapping, request/model-call association and recorded coverage. Inspect
   the same mapped content in CLIO and SPOTTER. Change the profile and confirm a new
   view of the same capture, with mass distinct from display intensity. Unsupported
   media coordinates remain unavailable. Retain the setup receipt and capture hash.

## 4. Broad live OPAL path

Create a fresh APPL-CORE session against the connected export, choose the explicit
qualified model, and enable SPOTTER. Open its reviewer beside the transcript.
The three prompts in [opal-live-inputs.json](opal-live-inputs.json) are the broad
onboarding, treatment-response and growth-curve path. They use the real export,
not the recorded-table chain below.

Run interactively or use the operator-side runner with the authenticated CLIO
endpoint/tunnel and this session's ID:

```sh
uv run --no-sync python scripts/qualification/beta3_opal_run.py \
  --endpoint http://127.0.0.1:8100 --session SESSION_ID \
  --input docs/qualification/beta3/opal-live-inputs.json \
  --receipt /operator-only/broad-run.json
```

Use `CLIO_QUALIFICATION_TOKEN` in the operator process if authentication requires
it; never put a credential in the URL or transcript. The runner pauses on permission
or user questions. Answer those in CLIO and resume with the same receipt. It pins
accepted message IDs and the explicit model; retrying never starts a substitute
session or guesses a reply by ordinal position.

Check the experiment card's stated/checked/inferred claims, units, exclusions,
verified loader/views and real chart selections. Inspect text, tools, artifacts,
image/A2UI selections and any availability explanations. Discuss findings in the
existing reviewer; Inspect evidence must return to the exact capture/profile/content.
Archive the transcript, references, artifacts, capture hashes and reviewer findings.

## 5. Controlled six-round chain

The supplied `opal_chains.csv` contains the six-round reference chain
`exp32-chain-0020-hard`. Prepare it on the **operator computer**:

```sh
uv run --no-sync python scripts/qualification/beta3_opal_inputs.py \
  --csv /operator-inputs/opal_chains.csv --output /operator-only/new-replay-folder
```

The command creates prompts-only `input-A.json` and `input-B.json`, plus a separate
`operator-manifest.json` with evaluator answers and the change location. It refuses
an incomplete chain or an existing output directory. Only round 5 differs. This
is explicitly labelled recorded-table replay; it does not pretend the supplied
table text came from live database tools. CSV `tool_ranges` are not trusted capture
coordinates; mapping uses the actual inference request and tokenizer.

1. Create two fresh parent sessions with the same explicit qualified model/settings
   and SPOTTER enabled. Keep evaluator files inaccessible to both agents. Do not
   tell the watcher which condition it observes.
2. Run `beta3_opal_run.py` for A and B separately, each with its own session and
   receipt. Maintain one actual six-turn conversation per condition; never inject
   expected answers as prior assistant messages.
3. Retain each completed round and fresh capture. Check the actual answer chain and
   downstream dependencies, then compare against the operator manifest **outside**
   the blind reviewer. A poisoning attempt need not succeed to be a valid run.
4. Ask the reviewer for grounded findings through its side panel. Record uncertainty
   and inspectable references. Attention strength alone is not proof of poisoning
   and must not cause automatic quarantine. Compare clean/changed runs after both
   blind reviews are retained.

The runner's `complete` means all model turns ended normally. Its
`capture_verified` and `reviewer_verdict_verified` remain false: a human must record
those separate inspections. Mock transport tests do not count as live inference.

## 6. Retain evidence and shut down

Export receipts with requested/effective configuration, revisions, operation IDs,
capture hashes and screenshots/recording locations; omit credentials. Keep models,
baseline manifests, service data and provenance evidence. Stop inference, CMF and
Flowcept through their management panels, verify observed state, reconnect once
and confirm no duplicate service was created. Stop the allocated-node Agent and
release the external scheduler allocation through the normal operator procedure.
Do not choose Delete retained data during this evidence-retention pass.

Only after fresh inference, both OPAL paths, grounded SPOTTER review, packaged
install/update checks and remaining live provider checks have evidence should the
owner decide whether beta 3 is ready to publish. No tag or publication is part of
this procedure.
