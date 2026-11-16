# Chemistry in CLIO — handoff (2026-11-16)

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

## Remaining
1. clio-kit: `fdmnes` and `graspa` servers (graspa tested against ChemGraph mock_graspa;
   no GPU), Spack recipes for gRASPA and FDMNES (sandbox-confined install tree/stage/caches),
   register all servers in `mcp-server-versions.toml`, a `clio-chemistry` bundle and
   per-server `plugins/clio-<name>/` (.mcp.json → `clio-kit mcp-server <name>`).
2. clio-agent: `catalogs/connectors/clio-kit/derive.yaml` (group `clio-chemistry: Chemistry`,
   names ASE/PubChem/gRASPA/FDMNES/DECIMER), hand-written `fdmnes.yaml`, `graspa.yaml`,
   `ase.yaml` needs, replace `burette.yaml` with the fork command + `plugin: burette`,
   bump the clio-kit pin and marketplace submodule once pushed.
3. marketplace: computational-chemistry (L1), ase, pubchem, graspa, fdmnes, decimer,
   nwchem, orca, burette plugins, alembic plugin + `agents/alembic.py`; `.lint-l3` +
   `lint-denylist.txt` on each.
4. Live qualification of the remaining C-cases (plain requests only), ledger.
5. Showcase screenshots for the website from every live case (deviceScaleFactor 2, at
   1440x900 and 390x844), saved under `site/src/assets/tutorials/chemistry/` per
   `site/WRITING.md`, alongside the ledger's evidence screenshots. Only screenshots of
   the real CLIO interface (Web or Desktop) go on the website; test-harness captures are
   evidence only.

## Not verified
- CLIO passing CA/proxy env to the pubchem server; ORCA, UMA, NWChem-via-Spack; Burette in live CLIO.
