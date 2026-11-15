# Footprints, part models and enclosures: implementation plan

This plan follows `plans/kicad-plugin-plan.md` and runs after it is done. It covers two things hobby boards keep running into.

- **A part has no footprint, or no 3D model.** The agent makes both from the datasheet with KiCad's own library generators, checks them against KiCad's library conventions and against the datasheet, and adds them to the project.
- **The board needs a case.** The agent measures the finished board from its STEP, writes a parametric enclosure in CadQuery around it, checks the fit in code, and shows the board and the case together in CLIO. The output is STEP to keep and STL to print.

Fusion is not used. CadQuery is the CAD path, because it runs headless, writes STEP and STL, and CLIO's agent already writes it well.

The plan has six workstreams. They are not ordered. The work is one goal that runs until every item under Done holds, and the human reviews the finished branches.

1. **The footprint skill.** Find, generate, check and assign a footprint and its 3D model from a datasheet.
2. **One footprint engine in the fork.** KiCad Library Tools replaces the fork's own generator and checkers.
3. **Bodies for every part.** No footprint on a board leaves the pipeline without a 3D body, so the board's STEP carries every height.
4. **The board envelope.** A script measures the board's STEP into one record the enclosure is built from.
5. **The enclosure skill.** A parametric CadQuery enclosure built from the envelope, with its fit checks.
6. **Seeing the fit.** The board and the case in CLIO's mesh viewport, with per-model opacity and comments that name the part under a mark.

## Contract

This plan is the contract for the six workstreams. When code and this plan disagree, change the code. If the plan turns out wrong, choose the fix that best keeps its intent, update this document in the same change, and record the decision in the ledger.

| Document | Governs |
| --- | --- |
| This plan | Footprint and model generation, the board envelope, the enclosure, the fit view |
| `docs/design/sdk/kicad-footprints-ledger.md` in clio-agent (new) | This plan's decisions and evidence |
| `plans/kicad-plugin-plan.md` (project) | The KiCad connector, the instance, the views, the `kicad` plugin and its skills. This plan adds to the plugin and changes nothing there |
| `plans/comments-and-visual-feedback-addendum-a.md` (project) | Region marks on an A2UI surface, including the mesh nodes under a mark |
| KiCad Library Conventions (klc.kicad.org) | What a correct footprint, symbol and model look like |

**Base.** `JaimeCernuda/kicad-mcp-pro` at the commit the KiCad plugin plan finished on. clio-agent, gact-tui and clio-agent-marketplace at the heads of that plan's branches. KiCad Library Tools at a pinned commit of `main`. The session writes all of them in the ledger header.

Decisions use the prefix `FP` for footprints and models, `EN` for the envelope and enclosure, and `FV` for the view. Qualification scenarios use `FQ`.

## Rules

The megachange rules and the rules of the KiCad plugin plan carry over. Three more apply here.

1. **The datasheet is the source.** Every pad, body dimension and pin number traces to a page of the datasheet ingested into clio-core, and the footprint's record cites it.
2. **Measured, not estimated.** Every number the enclosure uses comes from the board's STEP through the envelope script. The agent never types a component height or a hole position from memory or from a picture.
3. **The enclosure is a script.** The case is a CadQuery program with its parameters at the top, kept in the project. A board revision means running it again, not editing a mesh.

## Out of scope

- Fusion and every other interactive CAD tool.
- Slicing and printing. The handoff is STL and 3MF files in the workspace.
- Structural and thermal analysis of the case. That is in `plans/pcb-pipeline-future.md`.
- Contributing generated footprints back to KiCad's libraries.
- Symbols, beyond what `lib_generate_symbol_from_pintable` already does.
- Full PBR materials in the mesh viewport. FV2 adds opacity only.

## Current state

**The fork.**

- `lib_generate_footprint_ipc7351` in `tools/library_footprint_engineering.py` is the fork's own generator. Its docstring lists chip passives from 0201 to 2512, SOT-23, SOT-223, SOT-89, SOT-363, SC-70, SOD-123, SOD-323, SMA/SMB/SMC, DPAK, D2PAK, SOIC, SOP, SSOP, TSSOP, QFP, LQFP, TQFP, QFN, DFN, BGA and pin headers.
- The parity matrix row "Generate IPC-7351 footprint" says "SOT-23 only so far". That row is out of date with the code.
- `lib_validate_footprint_ipc7351` compares a two-terminal chip footprint with the IPC-7351B nominal only. `lib_certify_footprint` checks pad count against the package name, documentation layers and the recorded density.
- Library tools that stay: `lib_search_footprints`, `lib_get_footprint_info`, `lib_get_footprint_3d_model`, `lib_assign_footprint`, `lib_set_3d_model_path`, `lib_bulk_assign_3d_models`, `lib_search_3d_models`, `lib_generate_symbol_from_pintable`.
- `export_step` and `export_glb` write the board with its component bodies.
- `skills/kicad-design-review` shows how a skill in the fork carries `scripts/` and `references/`.

**KiCad Library Tools** (`gitlab.com/kicad/libraries/kicad-library-tools`, GPLv3). Its description lists footprint generators, symbol generators, 3D-model generators, KLC check scripts, housekeeping tools and CI tooling. It is the project the old `kicad-footprint-generator` address shows. The archived `kicad-packages3D-generator` says to use it instead. Footprints are generated by a script per package family from YAML dimension files, and the repository is driven by `./manage.sh`. The footprint framework is also on PyPI as `KicadModTree` 1.1.2. CadQuery is on PyPI at 2.8.0.

**Reachability.** gitlab.com refused this session's network (HTTP 403). A person's machine reaches it. A cloud session may not (see Owner work).

**CLIO.** `clio.mesh-viewport.v1` is a three.js viewport with no per-model material settings. The `visuals` plugin carries geometry-to-GLB. Region marks on an A2UI surface already report the mesh nodes under a mark (comments addendum A). The `manufacturing-processes` plugin covers build orientation and manufacturability for additive processes.

## Design: one footprint engine

**FP1. KiCad Library Tools is the engine.** It is what KiCad's own library is generated with, it follows the KLC, and it produces the 3D model with the footprint. The fork's homegrown geometry duplicates part of it, so the homegrown geometry is deleted.

| Removed from the fork | Replaced by |
| --- | --- |
| `lib_generate_footprint_ipc7351` and its geometry code | `lib_generate_footprint` |
| `lib_certify_footprint`, `lib_validate_footprint_ipc7351` | `lib_check_footprint` |
| The parity rows for both | New rows for the new tools |

**FP2. A separate process, not vendored code.** The fork is MIT and Library Tools is GPLv3. The fork never copies Library Tools code. On first use it clones Library Tools at the pinned commit into its own data folder, builds a virtual environment there from the repository's own requirements, and runs the generator and checker scripts as child processes. The pinned commit is a setting with a default, recorded in the ledger.

**FP3. `lib_generate_footprint(family, entry, output_library)`.**

- `family` names one of Library Tools' generator families, as the repository lists them at the pin. `lib_list_footprint_families` returns that list with each family's YAML schema and an example entry.
- `entry` is one YAML entry in that family's format: the dimensions from the datasheet's package drawing and recommended land pattern.
- The tool writes the entry to a project-local definition file, runs the family's generator, and writes the `.kicad_mod` and its STEP and WRL model into the project library (FP6).
- It returns the files written and the generator's own messages.

**FP4. A part with no generator family.** For an odd module or connector, the agent writes a KicadModTree script for that one part from the land pattern, and a CadQuery body from the package drawing. `lib_generate_footprint_custom(script, output_library)` runs it the same way, in the same environment. The footprint's record marks it `custom`, and the person reviews it with the datasheet drawing beside it (FP8).

**FP5. `lib_check_footprint(path, datasheet_ref)`.**

- It runs Library Tools' KLC footprint checker on the file and returns every violation with its rule.
- When `datasheet_ref` names the ingested datasheet and the land pattern extracted from it, it compares each pad's position, size and number with the datasheet within a tolerance (default 0.05 mm), and returns each mismatch.
- It passes only when both are clean, except for KLC rules about library naming, which don't apply to a project library and are reported as notes.

**FP6. Where the files go.** `<project>/libs/<project>.pretty` for footprints and `<project>/libs/3d/` for models, registered in the project's `fp-lib-table` with model paths relative to the project. The board stays portable as a folder.

## Design: the footprint skill

**FP7. `pcb-footprints`**, in the fork's `skills/`, copied into the `kicad` plugin as the KiCad plugin plan does for the fork's skills. It extends `datasheet-pin-check`. For every symbol on the schematic it works down this ladder:

1. **An existing footprint.** Search KiCad's libraries by the package name and the datasheet's package code. Accept one only after its pads match the datasheet drawing through `lib_check_footprint`.
2. **A generator family.** Pick the family, read its schema and example, and write the entry from the datasheet. Generate, then check.
3. **No family.** Write the custom script and body (FP4). Generate, then check.
4. **Assign.** `lib_assign_footprint` and `lib_set_3d_model_path`.

Each footprint gets a record in the project: the rung it came from, the datasheet pages it was taken from, and the check result. The pin check of the KiCad plugin plan uses the same datasheet pages.

**FP8. The person sees every generated footprint.** The skill renders the footprint (`kicad-cli fp export svg`) next to a crop of the datasheet's land pattern page and posts both. A `custom` footprint is not assigned until the person accepts it. A generated one is assigned and shown, and the person can reject it.

## Design: bodies for every part

**FP9. Coverage before the envelope.** The skill lists the board's footprints whose 3D model is missing or doesn't resolve (`lib_get_footprint_3d_model`). For each, it uses the Library Tools model when the footprint came from a family. Otherwise it builds a CadQuery envelope body from the datasheet's maximum dimensions, with a pin-1 mark, and records it as `envelope`. The enclosure work refuses to start while any footprint has no body, and names those parts.

## Design: the board envelope

**EN1. `board_envelope.py`**, a script in the `pcb-enclosure` skill (EN3). The agent runs it on the board's `export_step` output. It uses CadQuery to write `board-envelope.json` into the project:

| Field | What it holds |
| --- | --- |
| `source` | The STEP path, its hash, and the board revision it was exported at |
| `outline` | The board's outline as a polygon with arcs, and its thickness |
| `holes` | Each mounting hole's position, diameter and plating |
| `top`, `bottom` | Each side's maximum height and the part that sets it |
| `parts` | For each reference, its bounding box and side |
| `connectors` | For each connector, its mating face, the direction a plug enters, and the face's rectangle on the board edge |
| `keepouts` | Areas the case must stay clear of, such as an antenna area or an LED that needs a window |

- Connectors are found from the footprints whose library or reference marks them as connectors, and whose bodies reach or overhang the board edge.
- The keepouts come from the KiCad design's keepout zones plus what the person asks for.

**EN2. The envelope is an artifact with its source.** It names the STEP it was measured from. The enclosure script refuses an envelope whose source hash doesn't match the board's current export, and says to measure again.

## Design: the enclosure skill

**EN3. `pcb-enclosure`**, in the `kicad` plugin. It requires `manufacturing-processes`. It writes `<project>/enclosure/enclosure.py`, a CadQuery program:

- Parameters at the top: wall thickness, clearance to parts, clearance at cutouts, lid style (screws into standoffs, or snap fit), screw size, fillet radius, print orientation and nozzle size. Defaults suit an FDM print in PLA or PETG.
- It reads `board-envelope.json` and builds a base and a lid: the shell offset from the outline by the clearance, standoffs at each hole sized for the screw, a cutout through the wall for each connector's mating face plus its clearance, and windows for the keepouts the person asked for.
- It writes STEP and STL for each part, and 3MF for the slicer.
- It writes the assembled scene as GLB for the view, with each body named: `board`, each reference on the board, `base`, `lid`, each standoff and each cutout.

**EN4. Fit checks in the same run**, written to `enclosure/fit-report.json` and shown to the person:

- The minimum distance from the shell to every part. It must be at least the clearance.
- Each standoff's axis against its hole's position.
- Each cutout against its connector: a plug's envelope moved along the entry direction must pass through the cutout without touching the wall.
- That the lid clears the tallest top part, and the base clears the bottom side.
- Printability: walls at least twice the nozzle width, and overhangs past 45 degrees in the chosen orientation, each listed with its place.

A failing check names the part and the distance. The agent changes parameters and runs again. The run that is shown passes every check, or the agent says which check it couldn't meet and why.

**EN5. A board revision.** When the board changes, the agent exports the STEP, measures again and runs `enclosure.py` again. The script's parameters carry over, and the fit report is compared with the previous one.

## Design: seeing the fit

**FV1. The scene in the mesh viewport.** The agent opens the assembled GLB in `clio.mesh-viewport.v1`, in three states the person can switch between: assembled, exploded (the lid lifted along the vertical axis), and sectioned along a plane the agent or the person picks. The agent makes the section in CadQuery, not in the viewport.

**FV2. Opacity per model.** The viewport gains one property per mesh node, `opacity`, from 0 to 1. The skill sets the shell to about 0.3, so the board shows through. Nothing else about materials changes. The viewport's other uses are untouched.

**FV3. Comments name the part.** The GLB's node names (EN3) are what the region marks of addendum A report as the mesh nodes under a mark. "Move this hole" on a mark over a standoff reaches the agent as `standoff_2`, and a mark over the USB cutout as `cutout_J1`.

**FV4. The board view's 3D.** The KiCad board view's 3D toggle (KiCad plugin plan, KV11) keeps showing the board alone. The case is shown only in the mesh viewport.

## Defaults for the open questions

Take these and record each in the ledger.

| Question | Default |
| --- | --- |
| Library Tools pin | The `main` commit on the day the work starts |
| Where Library Tools lives | The fork's data folder, one clone per pin |
| Pad tolerance in `lib_check_footprint` | 0.05 mm |
| KLC naming rules in a project library | Reported as notes |
| A `custom` footprint | Assigned only after the person accepts it |
| Wall thickness | 2.0 mm |
| Clearance to parts | 1.0 mm |
| Clearance at a cutout | 0.5 mm per side |
| Lid style | M3 screws into standoffs |
| Nozzle | 0.4 mm |
| Overhang limit | 45 degrees |
| Shell opacity in the view | 0.3 |

## What the session installs

- KiCad 10, as for the KiCad plugin plan.
- CadQuery 2.8.0 and KicadModTree 1.1.2 in the fork's environment for Library Tools, plus what Library Tools' own requirements name at the pin.
- KiCad Library Tools at the pin.

## What the ledger records

- The Library Tools commit, the generator families it lists, the command each family runs, how it produces 3D models, and where its KLC checker is.
- How CadQuery exports the GLB scene with node names, and the CadQuery version used.
- The parity rows removed and added.
- Every footprint the qualification generated, with its rung, datasheet pages and check result.

## Qualification

Run focused tests in the fork and in the plugin while working, plus the fork's own checks for registry, parity and generated docs. Then run these against a freshly restarted stack and record each in the ledger with commands, handles and the screenshots read.

Fixtures: the design brief board from the KiCad plugin plan (USB-C, ESP32-C3, temperature sensor, status LED), and the datasheets of four parts: a QFN sensor variant with no footprint in KiCad's library, a JST connector variant with none, an odd module with no generator family, and a part that has a footprint but no 3D model.

**Footprints and bodies.**

| ID | Scenario | Passes when |
| --- | --- | --- |
| FQ1 | A part whose footprint is in KiCad's library | The library footprint is used after its pads pass the datasheet check. Nothing is generated |
| FQ2 | The QFN variant | A family entry is written from the datasheet. The footprint and STEP are generated. KLC is clean apart from naming notes. Every pad is within tolerance of the datasheet |
| FQ3 | The JST variant | Generated from the connector family, as in FQ2 |
| FQ4 | The odd module | A custom script and an envelope body. The person sees the footprint beside the datasheet's land pattern, and it is assigned only after acceptance |
| FQ5 | A datasheet whose land pattern has one pad 0.3 mm off from the generated footprint (an altered fixture) | `lib_check_footprint` fails and names the pad |
| FQ6 | The part with no 3D model | An envelope body is built and assigned. The board's STEP contains it |
| FQ7 | Start the enclosure with one body removed | The enclosure work refuses and names the part |
| FQ8 | Search the fork for `lib_generate_footprint_ipc7351`, `lib_certify_footprint` and `lib_validate_footprint_ipc7351` | Nothing outside the changelog |
| FQ9 | Search the fork for Library Tools code | None is copied. The tools run it as a child process |

**Envelope and enclosure.**

| ID | Scenario | Passes when |
| --- | --- | --- |
| FQ10 | Measure the brief's board | The envelope's outline, holes, side heights and USB-C mating face match values read independently from the KiCad board and the connector's datasheet |
| FQ11 | Build the enclosure with the defaults | STEP, STL and 3MF for base and lid are in the workspace. Every fit check passes |
| FQ12 | Set the clearance to 0 and the wall to 0.6 mm | The fit report fails the clearance and printability checks and names each place |
| FQ13 | Move J1 3 mm along the board edge in KiCad, then ask for the case again | The envelope is measured again, the cutout follows J1, every check passes, and the report shows what changed |
| FQ14 | Give the enclosure script an envelope from the old board | It refuses and says to measure again |
| FQ15 | The view: assembled, exploded and sectioned | Each renders in the mesh viewport. The shell is see-through at the default opacity |
| FQ16 | Mark the lid above the USB-C connector and write "make this opening bigger" | The agent's turn names `cutout_J1`. It changes the cutout clearance, runs again, and the check passes |
| FQ17 | Another surface that uses the mesh viewport with no opacity set | It renders as before |

Local phase, run by a person and recorded in the ledger:

| ID | Scenario | Passes when |
| --- | --- | --- |
| FQ-L1 | Print the FQ11 base and lid in PLA on an FDM printer | The board fits on the standoffs, the USB-C plug goes in, and the lid closes |
| FQ-L2 | Order the brief's board with the FQ2 and FQ3 footprints and assemble it | The parts solder onto the generated pads |

Nothing in the first two tables may be blocked. A failing scenario there is fixed, not deferred.

## Done

1. Every decision is implemented. The fork's own footprint generator and checkers are gone.
2. The fork's checks pass, and `ruff check` and `ruff format --check` are clean on touched paths.
3. Every scenario in the first two tables passes, each shown with commands, handles and the screenshots read. Local-phase rows say so.
4. The ledger holds every decision with its evidence and every fact listed above.
5. The site's user guide has a page on footprints from datasheets and one on enclosures.
6. The branches are pushed in the fork, gact-tui (FV2) and clio-agent-marketplace, with draft PRs carrying the summary and the evidence.

## Owner work beside this plan

| Item | Why |
| --- | --- |
| Allow `gitlab.com` in the cloud environment's network access | Library Tools is only on GitLab. Without it the session cannot clone it |
