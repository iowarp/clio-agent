# Footprints, part models and enclosures: ledger

This ledger holds the decisions and acceptance evidence for
[the footprints and enclosure plan](kicad-footprints-plan.md). Decisions use `FP`
(footprints and models), `EN` (envelope and enclosure) and `FV` (the view); qualification
rows use `FQ`. The person's messages are quoted in the appendix.

## Header

The base is where [the KiCad plan](kicad-plan.md) finishes. The heads are filled in when this
work starts.

| Repository | Branch | Base |
| --- | --- | --- |
| kicad-mcp-pro (fork) | `claude/magical-wozniak-ub0nxg` | the KiCad plan's last commit |
| clio-agent | `claude/magical-wozniak-ub0nxg` | the KiCad plan's last commit |
| gact-tui | `claude/magical-wozniak-ub0nxg` | the KiCad plan's last commit |
| clio-agent-marketplace | `claude/magical-wozniak-ub0nxg` | the KiCad plan's last commit |
| KiCad Library Tools | `main`, pinned | `090252c1f2429ceb8bd8ef2aa466b6c135f6273a` (`main` on 15 Nov 2026) |

## Reachability (15 Nov 2026)

- gitlab.com answers this session: `git ls-remote` on Library Tools returns `main`, so the
  plan's owner-work row is not needed here.
- PyPI has CadQuery 2.8.0 and KicadModTree 1.1.2.

## Decisions from the person's messages

| ID | Decision | Where | Status |
| --- | --- | --- | --- |
| FA-D1 | **The plan's mechanisms are a starting point; what works on a real board wins.** If a fixed measuring script cannot measure a board reliably, the agent writes the measurement itself from the board's STEP, and the skill teaches how to measure rather than which script to run. Where a render of the board or the case improves a measurement or a fit check, the skill says when to take one and what to look for. Each such change is recorded here and the plan is updated in the same change (message 1) | plan EN1, EN3, EN4; skills `pcb-enclosure`, `pcb-footprints` | open |
| FA-D2 | **The fork's own footprint tools are hidden from CLIO, not deleted** (message 2). `lib_generate_footprint_ipc7351` (generic IPC-7351B formulas, no 3D body, no connectors or modules), `lib_validate_footprint_ipc7351` (two-terminal chip passives against the IPC nominal) and `lib_certify_footprint` (pad count, documentation layers) duplicate a weaker part of Library Tools. Deleting them would change about 30 upstream files; as with the `sim_` tools (KiCad ledger KA6), the CLIO entry hides them and the fork's upstream code stays. FP1, FQ8 and Done 1 amended | plan FP1, FQ8, Done; clio-agent `catalogs/connectors/kicad.yaml` | open |
| FA-D3 | **The whole cycle is CLIO's; nothing built by hand counts** (messages 3 to 6). Installing, running, the MCP servers, viewing and putting it together are a CLIO run from a plain request: from nothing installed to a board, its reports and BOM, the board-house package and a case, with CLIO's own screenshots for the website. The session changes CLIO, the fork and the skills and watches; it does not install the toolchain, build models or bodies, run the scripts or render. Everything the session had installed was removed before that run (KiCad 10 and nightly with their apt sources, ngspice, KiCad 3D models, Library Tools and CadQuery in the scratchpad, CLIO's earlier FreeRouting), and the run uses a fresh CLIO profile | the full run (FQ-ALL below) | open |

## Decisions

| ID | Decision | Where | Evidence | Status |
| --- | --- | --- | --- | --- |
| FP-D1 | **Library Tools runs as a child process.** Fetched with `git` at the pin into `<tools>/library-tools/<pin>/repo` (the tools folder is the plugin's data folder under CLIO), installed into its own environment with `uv` (Python 3.12+); the 3D side (CadQuery) only when a model is asked for. `KICAD_MCP_LIBRARY_TOOLS` names a checkout the person made. When a step cannot complete, the tool says what the person does | fork `library_tools/install.py` | `test_install_says_what_the_person_does_when_it_cannot` | done |
| FP-D2 | **Facts about Library Tools at `090252c1`** (read, not run, from the clone): the runner is `src/generators/generate.py` (not `manage.sh`; `kilt_manage.py` only lints and installs); families are the 105 folders holding `spec.py`/`footprint.py`/`model.py`, 74 with a 3D generator; an entry is selected by its data file (`-c '*/<file>.yaml'`), and data must sit under `data/<family>/`, so the tool writes a uniquely named file there and removes it after; `-f`/`-m` choose the footprint and model folders; the file name comes from the entry's manufacturer and part number; STEP bodies come from the same entry; the KLC checker is `library_utils/klc-check/check_footprint.py` (text or JUnit output, exit 0/2/3; `-w` hides warnings, which separates errors); its `3d` extra pins **CadQuery 2.6.1**, not the plan's 2.8.0, so the scripts are written for CadQuery 2.6 or later | ledger | survey of the clone | done |
| FP-D3 | **Four tools.** `lib_list_footprint_families`, `lib_generate_footprint` (family entry → `libs/<lib>.pretty` and `libs/3d`, model paths under `${KIPRJMOD}`, library in the project's fp-lib-table, record in `libs/footprint-records.json`), `lib_generate_footprint_custom` (a KicadModTree/CadQuery script, marked custom, assigned only after acceptance), `lib_check_footprint` (KLC errors and warnings, naming rules G1.1/F9.1/F9.3 as notes, every datasheet pad within 0.05 mm, each mismatch named; takes a project file or `Library:Footprint`) | fork `tools/library_footprints_klt.py`, `library_tools/runner.py`, commit `1f73a8a` | `tests/unit/test_library_tools_footprints.py` (7, a stand-in checkout); registry, matrix, catalog, reference, surface and profile snapshots regenerated | done |
| FP-D4 | **The skills live in the CLIO plugin** (KiCad ledger KA10: the fork pins its own skill set): `pcb-footprints` and `pcb-enclosure` with `scripts/board_envelope.py` and `scripts/enclosure.py` | marketplace `plugins/kicad/skills/`, commit `5d5f165` | plugin tests: own skills name only live tools | done |
| EN-D1 | **The envelope is measured from the STEP.** The board solid's top face gives the outline (lines and arcs) and holes; each body (named by its reference in KiCad's STEP) its box and side; the board file only classifies (connectors, keepouts, plated holes) and lists footprints with no body, which stops the case (exit 3). Paths are kept relative to the envelope | `board_envelope.py` | development check on run 4's board; not evidence of the agent (FA-D3) | done |
| EN-D2 | **The case script and its checks.** Base with standoffs, lid with clamping pillars, an opening per connector, windows on request; checks name the part, the case element and the distance; `recess` reports how far a connector sits behind the outer surface (run 4's USB-C sits 0.95 mm inside the board edge, 3.95 mm behind the wall); a lid printed top-down gets a 45° chamfer, not a fillet; overhangs sample curved faces; 3MF through the exporter that supports it; the scene's nodes are named for marks, and the opening's frame is carved out of the base and lid so no two nodes share a surface | `enclosure.py` | development builds; not evidence of the agent (FA-D3) | done |
| FV-D1 | **`nodeOpacity` on `clio.mesh-viewport.v1`.** A map from node name to 0..1 (at most 256), in the catalog (a typed-map rule in the catalog renderer), the instructions, the client adapter and the scene: faded parts are draw groups with their own see-through material, opaque ones drawn first; a name the model lacks is reported in the view | clio-schemas `df33395`, gact-tui `6174c2c9` | schema tests 648 pass; mesh tests 29 pass; browser check pending the full run | done |

## Findings from the full run (run 2, `sess_8c249d9470a6`, workspace `sensor-node-board`, fresh profile)

| ID | Finding | What was done |
| --- | --- | --- |
| FN-1 | **System installs need the sandbox to elevate.** Run 1: the agent was granted the apt folders and still failed (the fenced child lacks root's privileges over apt's own files; the plain-HTTP 405 came from this cloud machine's HTTPS-only proxy, not CLIO), and the diagnosis told it to use `access_paths`. The person's rule: what the sandbox blocks is raised to them and allowing it extends the sandbox | `outside_sandbox` with a card that offers allow once or deny, in every approval mode; `privilege_denied`; cross-OS elevation messages (UAC, `osascript`, `pkexec`); clio-agent `e936a066`, `9054b86a`, `9b17f50a`. Run 2: the agent asked how to install, the person chose apt with 3D models, two cards were allowed, KiCad 10.0.8 with `kicad-packages3d` installed |
| FN-2 | **`ask_user` options with an `id` were refused** | the id becomes the value, `bda92ed5`; in run 2 the sensor answer came back as `lm75` |
| FN-3 | **The 3D model package nearly filled the disk** (several GB, plus CadQuery and Library Tools): the agent then deleted CLIO's MCP cache to free space, which removed the running KiCad connector's environment; every later schematic read failed (`Could not load schematic ... through kicad-sch-api`, the cause hidden) | the session freed its own leftovers; the person restarted the connector (disable, enable) and told the agent. Open: the shell's fence includes CLIO's whole user cache and config folders (`sandbox_roots.effective_write_roots`, `paths.user_cache_dir()`, `paths.user_config_dir()`), so an agent can delete a live server's environment or saved connector state; `install-kicad` should check free space before `kicad-packages3d` or fetch only the models a board names; the fork should name the underlying error |
| FN-4 | **The CLIO tab crashed** (`Target crashed`, `ERR_INSUFFICIENT_RESOURCES`) after about an hour of a long, busy conversation at device scale 2 | the driver reattached; open: look at the conversation view's memory in long sessions |
| FN-5 | **The driver missed on-click question cards** (`[data-slot="ask-row"]` buttons, no radio or Send) and answered every permission `allow_session` | driver fixed; `allowPending` answers with the broadest action a question offers (`06932cab`) |
| FN-6 | **`nodeOpacity` matched only a mesh's own name**, while CadQuery names the group (`base`, `lid`) and the mesh something else: the agent's see-through case drew opaque with "No part of this model is named base, lid" | gact-tui `e4b18f01`: a part keeps the names of the nodes above it and an opacity matches the nearest; the agent's own surface then rendered see-through (`site5-2-case-assembled.png`) |
| FN-7 | **The case script's clearance check trusted `Shape.distance`, which returned 0.0 for one pair 25 mm apart** (OCC); the agent found it, added a bounding-box lower bound in the project's copy and fixed its own first formula | open: port the bound into `pcb-enclosure/scripts/enclosure.py` |
| FN-8 | **Fork views lose their project**: the connector's project setting is gone after a restart and between turns, and a view reopened from the conversation fails ("No schematic file is configured") | open (fork): a view's saved call should carry its project; the agent re-set the project itself each time |
| FN-9 | **Fork board view**: Fit does not frame the whole board, and 3D stays on its spinner although `view_data_glb` answered in 1.5 s | open (fork views) |
| FN-10 | **Fork exports**: `export_3d_render` and the STEP export refused plain file names (their descriptions ask for one); a schematic tool's error hid the cause behind "Could not load schematic … through kicad-sch-api" | open (fork); the agent used `kicad-cli` |
| FN-11 | **A message typed while a turn runs is queued, not lost**: the person's typed message waited as "1 queued message" and arrived with the API-sent one, so the agent saw it twice | none needed; the driver should wait for the queue |

## Fixes after the full run (15 Nov 2026, the person's message 7)

The person asked what the open findings were, whether to fix them, and why the board render
was not inside CLIO (appendix, message 7). All were fixed; the render was outside CLIO because
both in-CLIO routes failed (FN-9 and FN-10), so the agent used `kicad-cli` in the shell.

| ID | Finding | Root cause | Fix | Evidence |
| --- | --- | --- | --- | --- |
| FX-1 | FN-3: the shell could delete CLIO's own runtime | `effective_write_roots` gave the shell all of `user_cache_dir()` and `user_config_dir()` (server environments, settings, hook trust, credentials, plugin store); the old test was vacuous (a parent of `mcp-uv-cache` was allowed) | those folders are the fleet's only; the shell keeps its workspace, grants, scratch, temp and the workspace's state and cache folders (its redirected caches), also on a node's narrowed path | clio-agent `c4655b96`; `test_the_shell_cannot_write_clio_runtime` (both paths); runtime/tools suites pass (20 errors in a first run were clio-core's 1 GB free-space preflight on a full disk; they pass with space restored) |
| FX-2 | FN-8: the project was lost after a server restart; a reopened view failed | the project lived only in the server's memory, and CLIO reaps an idle server after 120 s and rebuilds servers on connector changes | the fork records the last `kicad_set_project` per working folder (`KICAD_MCP_STATE_DIR`, the plugin's data folder in CLIO's entry) and picks it up when a fresh server is given none; opening tools return the project in `structuredContent.arguments` | fork `383d21d`; unit tests (restore, gone project not restored). Live, `sess_139b1f6ab804`: after 150 s away, "Show me the BOM for 5 boards." ran `view_bom` and `lib_get_bom_with_pricing` with no `kicad_set_project`; the view said "(The project was set again from this folder's last kicad_set_project.)"; after a reload the BOM view loaded with its data |
| FX-3 | FN-9: the board view's 3D spun forever | three faults: the model that arrived after the 3D tab opened was never announced; a load error was swallowed; and, the cause in CLIO, the viewer fetched the model's object URL, which the view sandbox's `connect-src 'self'` blocks ("Failed to fetch", shown once the error was no longer swallowed) | the view announces the model, gives each viewer its own URL, reports load errors, times out data calls, sends the model once (not twice), and the vendored viewer parses registered bytes from memory; `view_board(mode="3d")` and a `show` app tool let the agent open and look at 3D | fork `383d21d`, `8965bdc`; harness under the same CSP: 3D loads in 1 s and `show` returns a 100 KB PNG (development check, not evidence of the agent) |
| FX-4 | FN-9: Fit "did nothing" | Fit set the camera without drawing; in the live check the view was already fitted (the pink lines past the board are KiCad's drawing-sheet frame) | Fit draws, and refits when the view's size settles until the person moves the board | fork `383d21d`; harness: camera box equals the outline plus 10% before and after |
| FX-5 | FN-10: `export_3d_render`/`export_step` refused file names | CLIO's `ground_output_paths` turns `output_file="x.png"` into `<workspace>/x.png`; the fork refused any separator. The project sits in a subfolder of the workspace | the fork writes an absolute path inside its workspace exactly there; any other absolute path keeps its name in the output folder, and the answer names the path; relative paths with folders are still refused | fork `383d21d`; tests; live: the agent's `export_3d_render` wrote `sensor_node/output/3d/sensor_node_top.png` and looked at it |
| FX-6 | FN-10: a wrapped error hid its cause | `_clean_tool_error` read one exception level | the answer carries `Cause: <Type>: <message>` (three levels, 240 characters each) before `Hint:` | fork `383d21d`; tests |
| FX-7 | FN-7: `Shape.distance` 0.0 for a pair 25 mm apart | OCC distance | the clearance and opening checks take the bounding boxes' gap when the measured distance is below it, and say which was used | marketplace `9b1c5ca`; 3 tests |
| FX-8 | FN-3: the 3D models filled the disk | no guidance | `install-kicad` reads the package's installed size and the free space, tells the person, offers to skip the models; never deletes folders the agent did not make, nor CLIO's own | marketplace `9b1c5ca` |
| FX-9 | **The board in 3D inside CLIO, from a plain request** | | | live, `sess_203218a525e2`, a new conversation, "Show me this board in 3D.": the agent showed the board's GLB in CLIO's own 3D viewer (`create_a2ui_surface`), captured it and described it (board, USB-C, ESP32 with antenna, buttons, ICs, passives); screenshot `site3d-2-board3d-in-clio.png`. In `sess_139b1f6ab804` it opened the KiCad board view and an in-conversation render. Plain requests; evidence of the agent's choice |

New findings from these runs:

| ID | Finding | Status |
| --- | --- | --- |
| FN-12 | In a fresh conversation without the KiCad plugin, the agent said "I can ask you to add it" instead of raising the offer card; the person had to say yes before `offer_plugin` ran | open: agent judgement; the plugin offer guidance should make the card the way to ask |
| FN-13 | After a page reload, both KiCad cards' Show tab opened the BOM view; the board view's tab did not come back | open (gact-tui/CLIO app tabs) |
| FN-14 | **Conversations did not survive a restart of the isolated cloud stack**: after `stack.sh stop`/`start`, earlier sessions answered 404, boot ran the session-record migrations as if the store were new, and removed the old conversation's scratch (`session_gone`), though the clio-core files were on disk | open (clio-agent sessions/clio-core); needs its own investigation |
| FN-15 | Tab memory (FN-4): JS heap 128 to 295 MB across the 3D, BOM and reload steps, no crash | measured; the model is now sent once |

## The full run (FQ-ALL)

Plain request ("a small board for a sensor node … all the way to a package I can send to a board house, with the BOM, and design a 3D-printable case for the finished board. Show me the board and the case as you go"), fresh CLIO profile, nothing installed. Session `sess_8c249d9470a6`, workspace `sensor-node-board`, about 2 h 15 min with the stops above; three "use your judgement" nudges; the person answered two questions (install with apt and 3D models; keep the LM75BTP at 84 in stock), allowed two outside-the-sandbox installs, and restarted the KiCad connector once (FN-3). The agent installed KiCad 10.0.8 and its 3D models, found the module and regulator in stock, read four datasheets with page citations, generated the sensor's HWSON-8 footprint with Library Tools into the project library, drew the schematic (ERC 0), imported the outline, placed and autorouted with FreeRouting (231 tracks, 13 vias, ground pours with the antenna keepout clear), reviewed its renders, passed DRC (0) and the manufacturing gates, wrote Gerbers, drill, BOM and placement with a rotation fix, and built the case (`LID_HOLES` over the LED, buttons and sensor; the USB-C plug body from the USB-C specification; every fit check passing) shown assembled see-through, exploded and in section in CLIO's 3D viewport. The handoff lists what to check before ordering: rotations, the USB-C body and plug size as assumptions, the sensor's stock, its unconnected exposed pad, no simulation and no ESD protection. Website screenshots: `.cloud-run/shots/full2-7-kq32-progress-27m.png`, `site-2-outside-sandbox-card.png`, `full2u-7-view-18m.png`, `full2u-13-kq32-final.png`, `site5-2-case-assembled.png`, `site4-3-case-exploded.png`, and the agent's `fabrication/board_top_3d.png`. Evidence label: plain request; the person's interventions are listed.

## Qualification

| ID | Result | Evidence |
| --- | --- | --- |
| FQ8 | Pass | `scratchpad/fq8.py` builds CLIO's gateway from the saved KiCad connection (fork `491f4c9`, 16 Nov 2026): 400 KiCad tools reach the agent; none of `lib_generate_footprint_ipc7351`, `lib_certify_footprint`, `lib_validate_footprint_ipc7351` (hidden by the connector's declaration with `sim_*`, 14 tools, `connector_tools_hidden`); present: `lib_generate_footprint`, `lib_generate_footprint_custom`, `lib_check_footprint`, `lib_list_footprint_families`, `lib_get_footprint_3d_model`, `lib_assign_footprint` and the library readers |
| FQ9 | Pass | `grep` over the fork's `src/` and `packages/` (outside the vendored viewer): no import of `KicadModTree`, `kilibs` or the generator scripts; `library_tools/runner.py` runs Library Tools' own `generate.py`, `check_footprint.py` and one-off scripts as child processes in their own folder through `run_to_end.run`; `install.py` only checks that Library Tools' Python imports `KicadModTree`. `utils/footprint_gen.py` and `footprint_validate.py` are upstream's IPC-7351 code (`1487947`, before this branch), the tools FQ8 hides |

## Appendix: the person's messages

### Message 1 (15 Nov 2026)

> tho implement the plan based on wha tyou see, if the script idea fails and the agent is the
> one that needs to build the script to measure the distances that is okay, if an image taken
> from teh rendering works to improve that, then bring that up on the skill, etc

### Message 2 (15 Nov 2026)

Asked whether the old tools should be deleted (FP1) or hidden (the KiCad ruling on a small fork diff), after an explanation of what each does:

> hide them, go ahead

### Message 3 (15 Nov 2026), on the first case renders

> also that doesnt look right, also this isnt a screenshot inside clio

> You can delete is your box / Why not let Clio install them like in the real case / you sure clio is rendering this and reviewing it visually

### Message 4 (15 Nov 2026)

> but why are you not, if YOU are doing and NOT clio this is worthless

### Message 5 (15 Nov 2026)

> from install, to run, to mcp, to viewing, to putting it all together, this is a clio development cicle, if you isntall things, build things, or put thing rogther you are lying and cheating

### Message 6 (15 Nov 2026)

> yes, from isntall to a pcboard, reports, bom, case, etc that is the goal in one run with screenshots that then goes to the website

### Message 7 (15 Nov 2026), on the board render and the open findings

> So this bugs what are they? And should they be solved?
> Also, why is this view not inside Clio?
