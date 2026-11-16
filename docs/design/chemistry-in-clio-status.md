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
