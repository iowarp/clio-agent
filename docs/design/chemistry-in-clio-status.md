# Chemistry in CLIO — status (2026-11-16, second session)

Scope for the next session: the plan "Chemistry in CLIO: conclusions (Nov 15, 2026)"
with the parts listed in [chemistry-in-clio-deferred.md](chemistry-in-clio-deferred.md) removed. Bases: clio-agent `develop`,
clio-agent-marketplace `develop`, clio-kit `main` (4b5e6e3 — clio-agent pins
88874fbc from main; clio-kit `develop` is stale).

## Done in session_01BHQAwt26SXevc5vL25FMh8

| Repo | Branch | Commit | State |
|---|---|---|---|
| iowarp/clio-kit | claude/chemistry-in-clio | 0cec300 feat(pubchem), afa7dbf feat(decimer), 38c30df feat(ase) | pushed |
| JaimeCernuda/Burette | claude/chemistry-in-clio | c0f4ab9 (plist lint only where plutil exists), b258155..8e3513d clio-burette, 9b8f27f..77a4740 geometry editing | pushed |
| iowarp/clio-agent | claude/chemistry-in-clio | this status file and the deferred list | pushed |
| iowarp/clio-agent-marketplace | — | none | nothing changed yet |

### clio-kit servers (each its own package under `mcp-servers/<name>/`, uv.lock, tests, README with ChemGraph Apache-2.0 NOTICE)
- **ase** (7 tools): smiles_to_structure, inspect_structure, set_geometry, run_ase
  (energy/opt/vib/thermo/ir; constraints; writes result.json, opt_trajectory.xyz,
  opt_steps.csv, frequencies.csv, modes/mode_NNN.xyz 20-frame animations, IR CSVs),
  displace_along_mode, trajectory_frame, calculators. xTB/MACE/UMA are extras built on
  first use into cached worker envs (launcher syncs no extras). Tests 68 pass + 7 slow
  (75 pass with real xTB, MACE-OFF23 small, NWChem via apt in that container).
  Ethane eclipsed: one imaginary mode (xTB 293i, MACE 287i cm⁻¹); displaced+reoptimized
  → staggered, barrier 10.8 / 10.4 kJ/mol. ORCA and UMA: typed errors only.
- **pubchem**: resolve_name (candidates + `ambiguous` status; "xylene" → o/m/p),
  get_compound, fetch_structure. 20 tests incl. 2 live.
- **decimer**: image_to_smiles (RDKit check, confidence, "model's reading" note),
  crop_image. 18 tests incl. live readback (caffeine, aspirin exact). First call
  downloads 285 MiB weights (~2 min). Env 1.9 GB.

### Burette fork (`packages/clio-burette/`)
One stdio process: viewer + Ketcher views (same `ui://burette/...` URIs, but a focused
Mol* page instead of the hosted React shell), workspace files by path through a CLIO
loopback service, selection/region/frame/mode/focus via `ui/update-model-context` and
app tools, Ketcher round trip. Launcher: `uvx --from "git+https://github.com/JaimeCernuda/Burette@<sha>#subdirectory=packages/clio-burette" burette-clio`
(bundled Node from nodejs-wheel-binaries; no npm step). Checks: (a) one process — yes;
(b) Mol* 3D geometry editing — NO; the user chose a geometry-edit mode in the Burette
view (torsion/angle/bond handles, atom drag, server-side rigid fragment move, new file)
over a streamed Avogadro 2 — built at 77a4740: `set_geometry`/`move_atom` tools and an
Edit bar in the view, each edit a new `name.editN.ext` file reported to the agent
(ethylene glycol O-C-C-O 180°→60.0° measured from the written file; ring bonds refused); (c) unit cell for magnetite —
yes after server-side packing to P1. Not yet tried inside live CLIO.

## Done in session_01BHQAwsRFSfrkkofcujtmYL

| Repo | Branch | Commit | State |
|---|---|---|---|
| iowarp/clio-kit | claude/chemistry-in-clio | baf3c77 feat(fdmnes), 517dc52 feat(graspa), a9625c6 build(spack), 8f26ace feat(chemistry) registration + bundle, 0a33789 fdmnes manifests | pushed |

- **fdmnes** (4 tools): run_xanes, read_spectrum, compare_spectra, fdmnes_status. FDMNES
  found via FDMNES_COMMAND / FDMNES_EXE / PATH, never downloaded. 20 offline tests
  (stand-in executable replaying a recorded Cu K-edge) + 2 integration tests against
  real FDMNES (Cu fcc K edge: 1 inequivalent site, m3m, 16 s; FeO6 molecule read in Å),
  passing with both a hand build and the Spack build.
- **graspa** (5 tools): run_graspa, prepare_graspa (run folder for a GPU node),
  read_graspa, rank_frameworks (uptake or working capacity; incomplete frameworks
  excluded with the reason), graspa_status. Reads upstream gRASPA (block-average mol/kg
  table, adsorbate taken by name; `-nan` error bars) and gRASPA-SYCL output. 10 tests
  with stand-in executables replaying gRASPA's CO2-MFI example output and ChemGraph's
  SYCL format. **gRASPA itself never ran: no GPU here.**
- **Spack repo** `clio-kit/spack-repo/` (namespace clio, Spack 1.0 layout) + config scope
  confining install tree, stage and caches to `$CLIO_SPACK_ROOT` (no ~/.spack written).
  `spack install fdmnes` built and installed here (Spack 1.0.4); `graspa` +cuda/+rocm
  audit and concretize but were not built (no GPU toolchain).
- **Registration**: ase, decimer, fdmnes, graspa, pubchem 1.0.0 in `[servers]`, publish
  list, icons, prerequisites, SERVER_TAGS, README, CI Python lanes; bundle
  `clio-chemistry`; generated `plugins/clio-<name>/` and docs. Root suite 722 passed.
  Launcher starts fdmnes and graspa over stdio and answers a tool call.
  Generation also rewrote 22 unrelated docs pages and paraview's server.json (drift on
  main) — left out. Generated titles read "CLIO Pubchem", "CLIO Ase", "CLIO Fdmnes"
  (the generator title-cases names).

| iowarp/clio-kit | claude/chemistry-in-clio | e8d097c feat(fdmnes): fetch_materials_project (plan: "Materials Project fetch"; MP_API_KEY) | pushed |
| iowarp/clio-agent | claude/chemistry-in-clio | c5f53ca connectors (derive.yaml Chemistry group, ase/fdmnes/graspa entries, Burette fork entry), fa19ad1 / be5e99e marketplace pins, d788d71 MP key need; clio-kit pinned to e8d097c | pushed |
| iowarp/clio-agent-marketplace | claude/chemistry-in-clio (from develop f860020) | 7699fa3 Alembic agent + plugins, e5430e9 skills named as in the plan | pushed |

- **Marketplace** (item 3): `computational-chemistry` (L1: method-ladder, geometry-and-frequencies,
  thermochemistry, xanes, adsorption, chemistry-views); `ase`, `fdmnes`, `graspa`, `burette`
  follow their connectors; `pubchem`, `decimer` start clio-kit's servers (mcp.json, followed
  automatically); `nwchem` (nwchem-dft, install-nwchem), `orca` (orca-dft, install-orca,
  untested); `alembic` (mission + structure-from-name, molecule-properties, reaction-study,
  adsorption-screen, xanes-comparison); `agents/alembic.py` (ScienceAgent + all of them +
  slurm). Every plugin has `.lint-l3` and `lint-denylist.txt`. Checks: unittest OK, model pins
  OK, skill literals OK, `clio-agent plugin check` 80/80, clio-agent connector/plugin tests 441
  passed incl. the connector-following test, marketplace-marked tests 35 passed. Pre-existing
  failures in abaqus, kicad, visuals plugin tests are unchanged (visuals fails without these
  changes too).
- **Plan source:** the C-cases and skill names are from the plan doc "Chemistry in CLIO:
  conclusions" (Claude Docs artifact X6kLiZj1KKKABLg4rQKpm5), which is not in any repo.

### Avogadro 2 (item 4): feasibility, checked 2026-11-16
- **Install:** Ubuntu 24.04's archive has `avogadro` 1.99.0 (an Avogadro 2 pre-release;
  `/usr/bin/avogadro2`). The official 2.1.0 AppImage downloaded fine but running a downloaded
  prebuilt binary was refused by this session's permission policy, so 1.99 from apt was used.
- **Runs headless:** under Xvfb with `QT_QPA_PLATFORM=xcb LIBGL_ALWAYS_SOFTWARE=1` (Mesa),
  opening an XYZ. The AppImage ships only xcb/wayland platform plugins (no Qt VNC/WebGL), so
  streaming is X → VNC → WebSocket.
- **Streams:** `x11vnc -localhost` + `websockify` + noVNC (`apt install novnc`); in headless
  Chromium a drag in the noVNC page rotated the molecule (interactive, not just pixels).
- **Agent → Avogadro:** its RPC listener (MoleQueue JSON-RPC over the Unix socket
  `$TMPDIR/avogadro`, 4-byte big-endian length framing) accepts `openFile`, `loadMolecule`
  (format + content) and `saveGraphic` (PNG), all verified. The socket name is global per
  TMPDIR, so each instance needs its own TMPDIR.
- **Avogadro → agent, with the selection:** a Python command in
  `~/.local/share/OpenChemistry/Avogadro/commands/` ("Extensions → CLIO → Send to CLIO")
  receives the molecule as CJSON on stdin and writes it to a handoff folder; CJSON carries
  `atoms.selected` (per-atom booleans) — verified with Select All. The command must echo the
  `cjson` back, or Avogadro 1.99 replaces the molecule with an empty one (found and fixed).
- **Built (KiCad pattern, user direction 2026-11-16):** clio-kit 5a1a256 `avogadro` server —
  finds Avogadro (Where Avogadro is / `avogadro2` on PATH) and starts Xvfb + Avogadro (private
  TMPDIR/data, window sized to the screen, cwd `<workspace>/chemistry/avogadro`) + x11vnc +
  a token-checked loopback service (stream page, vendored noVNC 1.5.0, `state`, WebSocket
  bridge) declared as `ai.iowarp.clio/service`; tools avogadro_status, open_in_avogadro,
  read_avogadro, avogadro_snapshot, close_avogadro. It never installs: marketplace 0181ab8
  `avogadro` plugin's `install-avogadro` (official 2.1.0 AppImage + sha256, Xvfb/x11vnc via the
  package manager with CLIO's sandbox/elevation prompts) and `avogadro-building`; clio-agent
  2bfca4e `avogadro.yaml` connector. Semantics were worked out on Ubuntu's 1.99 package
  (integration test + headless Chromium through the stream: Select All, Send to CLIO, state
  reports the selection), then 1.99, x11vnc, noVNC, websockify, xdotool and their 92 new
  dependencies were purged so the live run starts with CLIO installing 2.1.0.
- **Found:** clio-agent's `DEFAULT_MARKETPLACE_COMMIT` (what an installed CLIO fetches) must
  follow the submodule pin; fixed in 95b83c6 (the earlier pin commits had not moved it).

## C-case ledger (live, real UI; plain requests unless marked)

Stack: `CLIO_CLOUD_ISOLATE=1 bash scripts/cloud/stack.sh start` (claude_code provider,
claude-sonnet-5-5), restarted after each marketplace pin. Driver:
`node scripts/cloud/ui_check.mjs --steps scripts/cloud/checks/apps/chem_case.mjs` (C1:
`checks/apps/lazy_agent_card.mjs`). Screenshots: `.cloud-run/shots/<label>-*.png` (not
committed). Results are judged against what the tools returned.

| Case | Size | Session | Result |
|---|---|---|---|
| C1 card | 1440x900 | sess_005e047a21c4 | **pass**: card named all 7 chemistry connectors off, each "downloads its tools"; Settings listed each |
| C1 enable all | 1440x900 | sess_005e047a21c4 | **partial**: ASE 7, Burette 14, DECIMER 2, FDMNES 5, PubChem 3 tools Ready; Avogadro and gRASPA failed in 4 s with clio-kit `--prepare` exit 1, shown as "does not know this server". Not reproduced (alone, concurrent in a fresh cache, or from the card again); the card dropped the command's output, fixed in clio-agent 3529ac5 |
| C1 turn 3 ("dock a ligand") | 1440x900 | sess_005e047a21c4 | n/a (docking deferred): asked for receptor, ligand and site and offered to install Vina itself with uv; no `offer_plugin` |
| C1 second conversation | 390x844 touch | sess_7bb1d072ae9f | **pass**: asked only about Avogadro and gRASPA; both Ready (5 tools each) |
| C2 (first runs) | 1440x900 | sess_deceefcdc5b1, sess_fb8c9c24735d | turn 1 **fail**: built all three isomers, no PubChem, no question. Cause: Alembic's mission never reached the prompt (gated on `spawn_skill_task`, a tool the main agent no longer has; marketplace be59cb0 fixed the gate) |
| C2 | 1440x900 | sess_7c6bedb1eeaf | **pass**: turn 1 loaded the study skill, `pubchem.resolve_name`, showed o/m/p with CIDs and asked; turn 2 PubChem 3D conformer (CID 7809) opened in Burette from the workspace file; turn 3 (methyl carbon selected in the view) answered from the selection (C7, its neighbours) with the methyl–methyl distance from `inspect_structure` (5.772 Å) |
| C3 | 1440x900 | sess_8ff77ee117fb | turn 1 **fail** on the view: xTB converged (17 steps), trajectory in Burette, convergence as a table, no linked chart (also after burette-views gained "In a dashboard"). Mechanism verified with a tool-directed prompt, not evidence of agent judgement: dashboard with the live view and energy/force charts, "frame linked both ways"; picking step 1 on the chart moved the view to frame 1. Turn 2 first attempt ended with no answer; retry **pass** (answered from the view's frame, bond changes computed from frames 0 and 1; it sees the view's frame, not the chart's selection). Turn 3 **pass**: MACE from the picked frame; refused to compare absolute xTB vs MACE energies and compared the two geometries with MACE single points (0.0055 eV) |
| C4 | 1440x900 | sess_ec012c4d1e75 | turn 1 **pass**: MACE-OFF23 opt of eclipsed ethane, one imaginary mode (287.29i cm⁻¹) named. Turn 2 **partial**: the mode played in Burette; the spectrum came as a static image, not a linked chart. Turn 3 **pass** (after the driver learned to press view buttons by selector; earlier coordinate drags rotated the camera): with a region drawn over one methyl, the answer named the marked atoms (C0, H2–H4) and measured their rotation from the frames in code (±13.9°, the other methyl counter-rotating). Turn 4 **pass**: displaced along the mode, re-optimized to staggered, no imaginary mode, lowest 295.78 cm⁻¹. Turn 5 **pass**: 0.1078 eV = 10.4 kJ/mol, same level, ZPE-corrected value also given |
| C5 (first runs) | 1440x900 | sess_3aaedc9841a1, sess_7abc2e1db239 | turns 1-2 **fail** on the view: correct numbers (O2 triplet, ΔG in code: xTB −1220.6, MACE −898.0 kJ/mol), diagram a matplotlib PNG. Cause: Alembic's mission ended "a figure when it helps" and chemistry-views said "chart" without saying a view is interactive; grounded in marketplace bfd3461 and 4ab0c5d. Also found: parallel MACE first runs collide on the model download (fixed, clio-kit 7567924). Two further attempts crashed the browser before sending (disk full), not counted |
| C5 | 1440x900 | sess_397172aefe0f | turn 1 **partial**: numbers as above, O2 triplet stated; no diagram ("I did not build the energy-diagram view"). Turn 2 **partial**: an interactive A2UI chart, but reactants and products only, values typed in rather than bound to the CSV, not live, no species-to-Burette link (the agent said so). Turn 3 asked as text (no O2 point to click): **pass**, triplet, and MACE has no spin input. Turn 4 **pass** on behaviour: asked before installing NWChem (`ask_user`; answered "Install with conda now"), installed it with micromamba into the workspace, set the connector path with `set_connector_value`, B3LYP/6-31G* on all four species: ΔG −624.9 kJ/mol, said it confirms neither earlier number and is basis-limited; read "two biggest disagreements" as xTB vs MACE and said why it ran every species. 45+ min. Turn 5 **pass**: reused the optimized geometries (frequencies and thermo only at 500 K), ΔG moves <1 kJ/mol, explained by Δn(gas)=0. Diagram not updated in turns 4-5. Overall **partial** (the live, per-species, linked diagram is the failing criterion) |
| C6 | 1440x900 | sess_3acc38e66e71 (sess_b2502919638c not counted: the driver sent turn 3 before the edit's permission prompt was answered) | turn 1 **partial**: MACE opt and frequencies (no imaginary mode), but it reported O–C–C–O 1.8° and "gauche with O–H···O"; the file is anti (−178.0°): it measured atoms 3-0-1-2. No view; a bridging "Show it to me." opened Burette. Turn 2 (the person, in Burette Edit mode: picked O0–C1–C2–O3, which read −178.0°, typed 60, Apply, allowed "Set geometry") wrote `final.edit1.xyz` at 60.0°. Turn 3 **partial**: re-optimized from the edited file (trajectory starts at 60.0°, ends 59.4°) with frequencies, gauche lower by 5.95 kJ/mol (5.7 with ZPE), and corrected its turn-1 claim from the view's geometry; it did not name the H-bond or measure it (closest O···H 2.71 Å, measured here), which the pass condition asks for |
| C7 | 1440x900 | sess_f09d31ce416f | turn 1 **fail** on DECIMER: an RDKit drawing of ibuprofen attached in the composer; the agent named it correctly (ibuprofen, SMILES, stereocentre unspecified) but read it by eye and said so ("did not run an image-to-SMILES tool"). DECIMER was reachable: the prompt listed the upload with the workspace_resource tools and a copy sat under `inputs/`; the agent chose not to (no `structure-from-image` load). Turn 2 **pass**: Ketcher opened seeded with the SMILES, the view reporting the structure. Turn 3 **pass** on the data flow: my click (bond tool) landed on C–OH instead of C=O and made `C(=O)=O`; the agent read the sketcher's structure, RDKit rejected it, and it said exactly what changed (the acid carbon double-bonded to both oxygens), proposed the fix and asked instead of guessing. The edit was a harness slip, not a chosen correction |
| C11 | 1440x900 | sess_61ab7b1d1c79 | **partial**. gRASPA: typed result (not installed; no GPU, `nvidia-smi` missing), fetched an HKUST-1 CIF (web search after the person allowed the Keenable source), prepared a GPU-node run folder with `prepare_graspa` and said how to finish and that the uncharged CIF will underestimate uptake. ORCA: `binary_missing`, checked PATH and folders, ran an xTB stand-in and said it is not comparable with ORCA. UMA: `hf_token_missing`, did not start the 2 GB environment, said not to paste the token in the chat. Failing criterion: the missing values were described as environment variables (`ORCA_COMMAND`, `HF_TOKEN`) instead of the card (`hf_token` is a secret need, `orca` an agent-settable value); ase-calculations now names `offer_plugin` needs and `set_connector_value` (marketplace aac2fc9). The gRASPA wrapper's tests pass against stand-in gRASPA executables (10 passed). Observed: answering the source-consent question reached the agent as an answer "for a question I never posted" |
| C8 (first runs) | 1440x900 | sess_cf22fb59de88, sess_17ee617b29d8 | turn 1 first run **fail**: the agent told the person to paste the key into the connector setting (fdmnes-xanes said only "ask through the connector"; now names `offer_plugin` needs, marketplace b6a3e03). Rerun **pass** on the card: `offer_plugin` raised "Give FDMNES Materials Project API key?"; the key was typed into the card's masked field (never in the chat, files or logs) and the fetch worked. Found: the fetch limited before sorting, so Fe3O4's cubic Fd-3m (0.013 eV/atom) was missing and C2/c reported as lowest (fixed, clio-kit 5fd25f9) |
| C8 | 1440x900 | sess_4e1260585219 | **partial**. Turn 1 **pass**: Fd-3m mp-aaaabcoo opened in Burette with its unit cell, choice explained (key already saved by the card). Turn 2: sites identified from the structure (tetrahedral Fe 0, 3 at 1.92 Å; octahedral Fe 1, 2, 4, 5 at 2.06 Å), one run per site planned with a radius check; FDMNES missing, it asked before a Spack install. The Spack build failed **because install-fdmnes clones clio-kit main, where the recipe is not yet released** (it found this and that upstream Spack has no fdmnes, and stopped to ask); a directed turn named the branch, CLIO built FDMNES with Spack in the workspace and set the connector path with `set_connector_value`. A container restart cut the runs; CLIO resumed the turn, the agent found the partial runs unusable and reran them under new names. The SCF runs did not converge (energy change about 7e4 eV after 31-37 cycles); it switched SCF off and said so. Found: the server's normalization took the tetrahedral pre-edge for E0 and gave the octahedral site an edge step of 0.013 (fixed: half-height E0, flat post-edge, clio-kit b6d7851); the agent had flagged that normalization as untrustworthy. Overlay: a first read-only A2UI chart, then (asked) a selectable one; the harness could not locate the chart to drag a range (the selection was not exercised), so turn 3 was asked without a selection: **partial**, a correct account from the computed pre-edges (0.30 vs 0.08) and site geometry, saying what was not verified. Turn 4 **pass**: with one Fe selected in Burette (Fe1), named the octahedral B site from the selection with 6 O at 2.058-2.060 Å (periodic neighbours) and corrected its earlier non-periodic count |
| C12 | 1440x900 | sess_397172aefe0f (the C5 conversation) | **pass** (with a gap seen later: on reopening, MCP App views of earlier turns stay folded inside their collapsed tool rows; only the newest view and A2UI charts show). After a stack restart the conversation reopened with its history; the energy chart drew again from its data (xTB to −1221 kJ/mol, method legend) with its table. "Show me the O2 structure again." opened the existing NWChem geometry in Burette (`burette_open_structure`, `inspect_structure`: O–O 1.215 Å measured on the file), no recomputation. C5 had no Burette view to come back. Also seen in C8: after a container restart CLIO resumed the running turn by itself, and the agent checked and reran the runs the restart cut |
| C10 | 1440x900 | sess_bd138d04f338 | **partial**. One plain request with five gas-phase reactions: per-species xTB and MACE thermochemistry (40 `run_ase` calls), reaction ΔG in code, and the agent decided from the results which pairs went to DFT (4 of 5 over 20 kJ/mol; H2 + Cl2 not). It asked before installing NWChem (`ask_user`, answered yes), installed it, fixed the conda build's hard-coded basis-library path with a wrapper, set the connector path, ran B3LYP/6-31G* on the nine species and said the small-basis DFT is itself far from textbook values, so the check does not say which method is right. Five `log_entry` writes to the study log. Failing criteria: it ran direct tool calls and `run_code`, not a flow it wrote and started; turn 2 ("Show me the reactions where the methods disagreed.") was answered from the conversation with no tool call, not from the study's records |
| Avogadro 2.1 (C6 conversation) | 1440x900 | sess_3acc38e66e71 | **pass** after three connector fixes. "Open the gauche conformer in Avogadro so I can rework it there." → `open_in_avogadro` reported not installed; the agent asked before installing and offered Burette. On "Yes, install it and open it." it loaded install-avogadro, installed the official 2.1.0 AppImage (checksum matched) into `~/.local/opt/avogadro-2.1.0`, plus x11vnc and the missing OpenGL libraries, and set the connector path. The streamed view then failed three ways, each fixed in clio-kit: (1) 2.1 shows a first-run dialog before starting its RPC listener (launch with `--skip-dialogs`, 5c600c9; `--rpc-name` made 2.1 fail to listen); (2) under the sandbox's deep TMPDIR the socket path was 122 bytes, over the 107-byte limit (EINVAL; the socket folder moves under /tmp, b099796); (3) noVNC's vendored pako was never committed because the repository ignores `lib/` (view blank; tracked, packaged and tested, 931d627). Now the full Avogadro 2.1 window streams in the conversation with the conformer loaded (`avogadro-stream.png`), and the agent's RPC snapshots render. Not exercised: an edit in Avogadro sent back with Send to CLIO |

## Remaining
1. ~~clio-kit servers, Spack recipes, registration, bundle, plugins~~ — done (above).
2. ~~clio-agent connectors and pins~~ — done (above). Was: `catalogs/connectors/clio-kit/derive.yaml` (group `clio-chemistry: Chemistry`,
   names ASE/PubChem/gRASPA/FDMNES/DECIMER), hand-written `fdmnes.yaml`, `graspa.yaml`,
   `ase.yaml` needs, replace `burette.yaml` with the fork command + `plugin: burette`,
   bump the clio-kit pin and marketplace submodule once pushed.
3. ~~marketplace~~ — done (above). Was: computational-chemistry (L1), ase, pubchem, graspa, fdmnes, decimer,
   nwchem, orca, burette plugins, alembic plugin + `agents/alembic.py`; `.lint-l3` +
   `lint-denylist.txt` on each.
4. ~~Avogadro 2 as a streamed view~~ — built (above); 2.1.0 to be installed and run by CLIO in the live cases. Was:, the way ParaView is streamed (user decision, 2026-11-16:
   the goal is a good CLIO chemistry stack, not only the SC demos). Burette stays the light
   in-conversation viewer and quick internal-coordinate editor; Avogadro 2 is for building
   and remodelling in 3D (fragments, element changes, auto-optimize while dragging, crystals,
   surfaces, supercells). First check it installs and streams on a Linux node; its saved
   file returns to the agent as a workspace structure; work out how its selection reaches
   the agent; add a burette-views/avogadro skill on which to use when.
5. Live qualification of the remaining C-cases (plain requests only), ledger.
6. Showcase screenshots for the website from every live case (deviceScaleFactor 2, at
   1440x900 and 390x844), saved under `site/src/assets/tutorials/chemistry/` per
   `site/WRITING.md`, alongside the ledger's evidence screenshots. Only screenshots of
   the real CLIO interface (Web or Desktop) go on the website; test-harness captures are
   evidence only.

## Not verified
- CLIO passing CA/proxy env to the pubchem server; ORCA, UMA, NWChem-via-Spack; Burette in live CLIO.

### Showcase captures (live UI, deviceScaleFactor 2)
Saved in `site/src/assets/tutorials/chemistry/`: `magnetite-unit-cell.png` (C8, the Fd-3m cell in Burette), `imaginary-mode.png` (C4, the imaginary mode playing), `dihedral-edit.png` (C6, Edit mode with O0–C1–C2–O3 picked), `avogadro-stream.png` (Avogadro 2.1 streamed in the conversation, installed by CLIO), and at 390x844 `magnetite-phone.png` and `imaginary-mode-phone.png`. Each view was reopened with a plain request in its own conversation and captured as an element. Not used: the Ketcher capture (seeded from the 3D file, it drew every hydrogen and a tangled carboxyl). Observed at 390 px: the Burette view's toolbar is clipped on the right and two icons overlap (Burette fork). Not captured: the FDMNES key card (answered before captures), the C5 energy chart and the XANES overlay (A2UI charts; the harness could not locate them to crop).
