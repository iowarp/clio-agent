# KiCad in CLIO: ledger

This ledger holds the decisions and acceptance evidence for [the KiCad plan](kicad-plan.md).
Decisions follow the plan's prefixes. The decisions in the `KA` rows come from the person's
messages of 15 November 2026, which are quoted in full in the appendix.

## Header

| Repository | Branch | Base |
| --- | --- | --- |
| clio-agent | `claude/magical-wozniak-ub0nxg` | `2ad7cd5c`, the head of `claude/parallel-agents-plan-uzpzm0` (the MCP Apps work) |
| gact-tui | `claude/magical-wozniak-ub0nxg` | `ab113365`, the head of `claude/parallel-agents-plan-uzpzm0` |
| clio-agent-marketplace | `claude/magical-wozniak-ub0nxg` | `d5ca114`, the head of `claude/parallel-agents-plan-uzpzm0`. The plan names `develop` `cea9142`, which this head does not contain. The MCP Apps branch is the base because the plugin needs the `follows` and surfaces work |
| clio-schemas | `claude/magical-wozniak-ub0nxg` | `3b9c5e4` (no change planned) |
| JaimeCernuda/kicad-mcp-pro | `claude/magical-wozniak-ub0nxg` | `d679d9ee` (release 4.16.0), still the head of `main` |
| Huaqiu-Electronics/ecad-viewer | vendored, read only | `6f9fbf19504d1bd0f07c1f91f4b6a8d69cd07467` (6 Nov 2026) |

## The machine

| What | Version or source | Note |
| --- | --- | --- |
| KiCad 10 | `10.0.8~ubuntu24.04.1` from `ppa:kicad/kicad-10.0-releases` | `add-apt-repository` fails here because the system Python has no `apt_pkg`. The PPA list and its key (`FDA854F61C4D0D9572BB95E5245D5502FAD7A805`, read from the Launchpad API) were added by hand |
| KiCad nightly | `10.99.0` from `ppa:kicad/kicad-dev-nightly`, installed beside KiCad 10 (`kicad-cli-nightly`, `pcbnew-nightly`) | `kicad-cli-nightly api-server [--socket PATH] PROJECT_OR_FILE` exists, so KQ7 runs here |
| ngspice | not installed from apt | The `ngspice` package conflicts with `libngspice-kicad` over `/usr/lib/x86_64-linux-gnu/ngspice/analog.cm`. The spice connector's own runtime is used |
| 3D models | not installed yet | Disk allowance. Until installed, the GLB is the board without component models |
| Xvfb, Java 21, Docker, node 22, pnpm | present | |
| FreeRouting | the image `ghcr.io/freerouting/freerouting:2.1.0` | The release JAR cannot be fetched from github.com in this session |

## Decisions from the person's messages (15 Nov 2026)

| ID | Decision | Source | Where | Status |
| --- | --- | --- | --- | --- |
| KA1 | The BOM pricing tool is `lib_get_bom_with_pricing` (KV12, current state). The plan is corrected in place | Appendix, message 1, item 1 | plan KV12 | done |
| KA2 | Part specs: `ComponentRecord.specs`, normalized units, `lib_get_component_specs`, `lib_search_parametric`, `lib_compare_parts`, and `lib_check_derating` reading `specs` (KT1, KT1a) | Message 1, item 2 | fork `utils/component_search.py`, new tools | open |
| KA3 | `datasheet-pin-check` takes missing values from the ingested datasheet with the page cited, and builds missing symbols with `lib_generate_symbol_from_pintable` (KP1) | Message 1, item 3 | fork `skills/datasheet-pin-check` | open |
| KA4 | A sixth view, `ui://kicad/parts`, with `view_parts`, `view_data_parts`, search, compare and pick, `selection`, `select_row`; BOM rows show key specs; the parts selection joins `/pcb/selection` (KV2, KV4, KV5, KV8, KV12, KV15, KW1) | Message 1, item 4 | fork views | open |
| KA5 | Qualification rows KQ37 to KQ41 | Message 1, item 5 | this ledger | open |
| KA6 | **The fork's diff stays small and upstreamable.** New behaviour goes in new modules; existing files get registration lines and the additive `specs` work only; nothing in the fork names CLIO. KT2 is amended: the `sim_*` tools are not deleted, because deleting them would conflict with every upstream change. The entry hides them from CLIO, and KQ36 becomes "no `sim_` tool reaches CLIO's agent". The diff against upstream is measured at the end | Message 2 ("I really would prefer to change the source the least amount possible…") | plan KT2, KQ36; fork | open |
| KA7 | **Asking for sourcing keys** (KC5, KT1b). On enable, the cards show the optional needs as "Part catalogs". At run time, the fork's result says which sources could be set up, and CLIO's `offer_plugin(plugin, reason, needs=[…])` raises the setup card for an enabled connector with just those rows. Secrets go to the credential store, the server restarts, and Not now is a typed refusal. This is not MCP elicitation: the specification forbids form-mode elicitation for secrets, and URL mode would keep the keys outside CLIO's store. The person's ideal was elicitation, with the tool-result hint plus a CLIO request as the alternative. The alternative was chosen because it needs one small, generic change in the fork and none in its protocol layer | Message 2 | clio-agent `connector_offer.py`, gact-tui setup card, fork sourcing | open |
| KA8 | No real Nexar, DigiKey or Mouser keys are available. KQ4 and KQ38 move to the local phase. KQ42 covers the asking flow with jlcsearch unreachable and a dummy key | Message 2 ("No keys") | qualification | open |
| KA10 | **New skills live in the CLIO plugin, not the fork.** The fork pins its public skill set at seven: its CI workflow (`agent-runtime-config.yml`), `scripts/sync_agent_skills.py` and several tests (`test_agent_skills_distribution.py`, `test_skill_installation.py`, `test_local_quickstart.py`) count the skills in `skills/`. Adding one touches all of them, so `install-kicad`, `pcb-placement`, `pcb-routing` and `datasheet-pin-check` are written host-neutral in `plugins/kicad/skills/`. `pcb-design` stays as upstream has it. This amends KP1's "Lives in" column | KA6 | plugin `skills/` | open |
| KA11 | **An install skill** (`install-kicad`), with how to run KiCad headless and the fact that KiCad has no flag or variable that skips its first-run wizard: it is skipped by seeding the settings folder | Message 4 ("you should build an install KiCad skill… specially if there is any parameter to skip it if running headless") | plugin `skills/install-kicad` | open |
| KA9 | KQ43: with KiCad removed, the agent installs it through the sandbox's folder-access card, which was built for installing and launching programs such as headless KiCad | Message 3 | qualification | open |

## Findings

| ID | Finding | Status |
| --- | --- | --- |
| KN1 | `runtime/sandbox_diagnosis.py` recognizes elevation only through Windows exit 740 and "requires elevation". On Linux and macOS, `sudo` with no terminal fails with "a terminal is required to read the password" (or "a password is required"), and this is not diagnosed as `elevation_required`. CLIO never takes a sudo or administrator password, by design (no elevated lane). A future "elevated run" card could start the command through the OS's own prompt (`pkexec`, `osascript … with administrator privileges`, UAC `runas`), so the password goes only to the OS. That is not part of this plan | reported to the person |
| KN2 | *Corrected.* First recorded as "a repin never reaches someone who already enabled KiCad". Tested: a conversation's server is built from the entry as it is now (`fill_declaration(..., from_entry=True)` in `plugins/servers.py`), so the repin did reach conversations after a restart, and turning KiCad off and on was not needed. What did stay stale was the saved copy in `mcp.yaml`, which the sign-in address (`materialize.in_use`), CLIO's server settings and the person read: after an entry moves a URL connector's address, a sign-in would store tokens under the old address while conversations call the new one | fixed (CD-K4) |
| KN3 | CLIO's fence leaves the home folder read-only to connector servers (fleet write roots: workspace, temp, CLIO's own state and caches, tool caches). The fork kept the managed KiCad's config home in `~/.cache/kicad-mcp`, so under CLIO the instance never started ("Read-only file system"), every IPC tool failed, and the agent fell back to editing the board file with `fs_edit_file` (block 1, first run, session `sess_72ea4e5e54cf`). Fixed in the fork (KI-D6) | fixed |
| KN4 | A view-only tool that names its `ui://` resource (the fork's `view_data_*` and `view_wait_revision`, as MCP Apps allows) registered a new app instance and wrote an "Open in a tab" card on every call the view made: one board view left 13 tabs and 20 cards in the conversation, and the test harness read a stale instance. The MC-F3 rule dropped only the call and result rows | fixed in CLIO (CD-K1) |

## Decisions

Implementation decisions are added here as they are made.

| ID | Decision | Where | Evidence | Status |
| --- | --- | --- | --- | --- |
| KI-D1 | **Attach or start, opt-in.** `KICAD_MCP_KICAD_INSTANCE=attach` (the default, upstream's behaviour) only connects; `auto` makes the first tool that needs IPC call `instance.ensure()` from `connection.get_kicad`/`get_board` on a connection failure, then retry once. Order: a KiCad on the configured or default socket with this board open (`attached`), `kicad-cli api-server` when a CLI offers it (`headless`), `pcbnew` under `xvfb-run -a -s "-screen 0 1600x1000x24"` with no display (`xvfb`), `pcbnew` with a display (`window`). `KICAD_CLI_PATH` set explicitly restricts the search to that CLI | fork `src/kicad_mcp/instance/`, `connection.py` (two 8-line wrappers) | `tests/unit/test_kicad_instance.py` (12); live below | done |
| KI-D2 | **The API setting and the first-run wizard.** The setting is `api.enable_server` in `<KICAD_CONFIG_HOME>/<major.minor>/kicad_common.json`. KiCad has no flag or environment variable that skips the first-run wizard (`KICAD_LOCKING` and `KICAD_API_SERVER` are trace masks only); while the wizard is open every call answers "KiCad is not ready to reply". It is skipped by seeding `kicad_common.json` (API on, `do_not_show_again.data_collection_prompt` and `update_check_prompt` true), `sym-lib-table` and `fp-lib-table` from the template folder, and `design-block-lib-table`, for which KiCad 10.0.8 ships no template: an empty `(design_block_lib_table (version 7))` is the file whose absence kept the wizard up | fork `instance/config_home.py` | screenshots `kiinst/screen*.png` (wizard, then the board), cold start in a fresh folder reached the API (`live1/home2`) | done |
| KI-D3 | **Sockets.** `kicad-cli api-server --socket` takes a plain path (an `ipc://` value is taken as a relative path and creates `ipc:/…` folders); clients use `ipc://<path>`. The GUI `pcbnew` ignores `KICAD_API_SOCKET` and serves `/tmp/kicad/api.sock` (`%TEMP%\kicad\api.sock` on Windows); the server finds it by probing for the board | fork `instance/manager.py` | live probes | done |
| KI-D4 | **Stale locks.** A killed KiCad leaves `~<board>.kicad_pcb.lck` and `.kicad_pro.lck` holding `{"hostname","username"}`, no process id; the next start shows "already open by root at vm" and blocks. A lock naming this host is removed when no KiCad process has the board open | `clear_stale_locks` | test + live | done |
| KI-D5 | **Lifetime.** A started KiCad is its own process group, stopped on server exit (`atexit`, SIGTERM then SIGKILL); an attached one is never stopped | `InstanceManager.stop` | after both live runs `pgrep -af "api-server|pcbnew|Xvfb"` found nothing (KQ9 evidence, mechanism) | done |
| KI-D6 | **The managed instance's config home is in the per-user temp folder** (`<tempdir>/kicad-mcp-<user>/instance`), not `~/.cache`. It is seeded on every start and belongs to a child the server stops, so nothing in it outlives the server; temp is writable on every OS and inside sandboxes that keep the home folder read-only (KN3). `KICAD_MCP_INSTANCE_HOME` still overrides it | fork `instance/manager.py::_home` (one function), commit `ae59c57` | instance unit tests (29) pass; live rerun below | done |
| CD-K1 | **A view's own call to a view-only tool opens nothing.** The MCP App observer skips it with the typed reason `mcp_app_skipped_view_only_call` (high-volume ring, like `no_resource_uri`), before it registers an app. Domain-free: any server's polling view benefits | clio-agent `gact/mcp_apps.py` (3 lines), `gact/mcp_app_observer_reasons.py`, commit `0e250d13` | `test_a_view_only_call_naming_its_view_opens_no_new_view` fails without the fix (3 cards) and passes with it; 200 MCP-app/observer tests pass | done |
| KI-D7 | **A started KiCad window is reached on its temp-folder socket.** KiCad's window serves `$TMPDIR/kicad/api.sock`, not a fixed `/tmp/kicad`; the window inherits the server's temp folder, and CLIO's fence points a connector's temp folder at CLIO's per-workspace sandbox cache. `started_socket()` is used for windows the server starts; `default_socket()` (kipy's default) stays for attaching to the person's own KiCad. This corrects KI-D3's "serves `/tmp/kicad/api.sock`" for any process whose temp folder is moved | fork `instance/manager.py`, commit `92c24b6` | `test_a_started_window_serves_its_socket_under_the_temp_folder`; live: `pcbnew` served `.cloud-run/xdg/cache/clio-agent/sandbox/<ws>/kicad/api.sock` (its `TMP`), and the server waited on `/tmp/kicad/api.sock` until this fix | done |
| KV-D1 | **`view_board` says each call opens another view, and that an open view is steered in place by its own tools** (`focus`, `set_layers`, `select`, `capture`). Grounding, not a constraint: in block 1 the agent answered "Show me C1" by opening a second view with `focus` instead of focusing the open one | fork `views/__init__.py` docstring, commit `fc69230`; regenerated tool reference and capability catalog | KQ28 rerun | done |
| CD-K2 | **A connector's offered tools leave out what `hide_tools` hides** (`connectors/listing.py::offered`). The Extensions row said "410 tools" while the agent got 399; the readiness listings read the same function | clio-agent commit `b66cb8dd` | `test_offered_leaves_out_the_tools_the_entry_hides` | done |
| CD-K3 | **Core comments name no connector** (KQ31): four comments that cited "KiCad plan" or used KiCad as an example now say "connector plan" | clio-agent, gact-tui comments | KQ31 below | done |
| CD-K4 | **A saved connector connection follows its entry.** `connectors/refresh.py` compares an enabled connector's saved copy with `materialize.declaration(entry)` on the template fields (transport, command, args, env, url, headers) and rewrites it with `save_connection` when they differ, with the typed trace reason `connector_connection_refreshed` and the changed fields. Values (connector state) and secrets (credential store) are not in the template and stay; a sign-in's block is kept. The Extensions listing (`connectors_service.rows`) calls it for every enabled connector. Making the entry the template on every fill path was tried first and broke the fleet's restart detection, which compares specs built from the saved copy, so that path keeps the copy and the copy is kept current | clio-agent `connectors/refresh.py`, `gact/connectors_service.py` (3 lines) | `tests/test_connectors/test_refresh.py` (4); connector and plugin tests 399 pass, the one failure is the pre-existing `test_grounds_entries` geo case | done |
| KT-D1 | **Moving a footprint to the other side** is the fork's existing `pcb_set_footprint_layer`; no new side tool | plan KT1, KV10 | `pcb.py:3928` | done |
| KT-D2 | **Locking** sets the footprint proto's `locked` (`LockedState`) over IPC and reads it back; kipy 0.8 has no `locked` property on `FootprintInstance` | fork `tools/pcb_outline_tools.py` | live: R1 locked, then unlocked, on `clean-led-kicad10` (headless) | done |
| KT-D3 | **DXF.** The fork has no DXF library, so a small reader parses ASCII DXF: LINE, ARC, CIRCLE, LWPOLYLINE with bulges, R12 POLYLINE; `$INSUNITS`; Y flipped. Circles are holes, drawn as Edge.Cuts circles (non-plated cut-outs), or only listed (`holes="none"`). Both outline tools refuse to overwrite an outline unless `replace` | fork `pcb/dxf_outline.py`, `pcb_outline_tools.py` | `test_pcb_outline_tools.py` (9); live: 60 x 40 mm enclosure DXF with 3 mm corners and four M3 holes → "8 edges and 4 holes … 60.00 x 40.00 mm", the first call refused, `replace` drew it | done |
| KT-D4 | **Specs.** `PartSpec(name, display, number, unit, source, label)`; numbers in SI base units; canonical names through an alias table; jlcsearch's category endpoints read for typed columns (`capacitance_farads` → `capacitance` F, `tolerance_fraction` → %, `voltage_rating` V …) and the `attributes` JSON; the search API has only a description, so values parsed from it are marked `<source>:description`. jlcsearch filters on the server only where its pages take a parameter (capacitors `capacitance` in farads, resistors `resistance`, ldos `output_voltage`, leds `color`, microcontrollers `core`); every other filter applies to the returned page (100 rows) | fork `utils/part_specs.py`, `library/parametric.py` | `test_library_parametric.py` (22); live jlcsearch: "10 µF ≥25 V X7R 0805" → GRM21BZ71E106KE15L (C237493, stock 75633) | done |
| KT-D5 | **Keyed sources in parametric search.** Nexar, DigiKey and Mouser are asked by keyword (category plus the filter values) and filtered on their returned specs; their native parametric filters need per-source attribute ids and are not mapped | `ParametricService.search` | unit tests with injected clients; no keys (KA8) | done |
| KT-D6 | **The sourcing hint.** Every failed sourcing result except a missing file ends with the key-holding sources that could be configured, their variables and what each adds; the new tools also list `answered_by` and each failed source with its reason | `library/parametric.py::sourcing_hint`, 9 messages in `library/sourcing.py` | `test_failed_sourcing_results_name_the_sources…`, the existing sourcing tests unchanged | done |
| KT-D7 | **Derating from specs.** `lib_check_derating(kind, parameter, rated_value, operating_value, …, part="")`: `rated_value` keeps its position and may be null, and then the rating comes from `part`'s specs (`voltage` → `voltage_rating`, …) with the source named. Reordering the arguments broke the fork's positional callers, so the order is kept | fork `tools/library_sourcing.py` | `test_library_sourcing_registration.py` passes unchanged | done |
| KT-D8 | **Fork baseline.** Four `test_reference_pcb_ipc_process_attestation.py` tests fail at `d679d9e` in this container because it runs as root (`ipc_peer_not_dedicated_user`); they fail the same with every change here | — | full unit run at base and after | noted |
| KT-D9 | **Fork diff so far:** `src/kicad_mcp` 17 files, +2316/−12 against `d679d9e`; edits to existing files are registration lines, the additive `specs` field and its four extraction lines, the two connection wrappers, the derating parameter and the hint suffixes; the rest is new modules and regenerated files | fork | `git diff --stat d679d9e -- src/kicad_mcp` | done |

## Qualification

| ID | Commands and handles | KI2 case | Screenshots read | Evidence label | Result |
| --- | --- | --- | --- | --- | --- |
| KQ31 | `grep -rniE "kicad|kicad_pcb|view_board|view_data_|view_parts" --include=*.py src` in clio-agent; the same over `web packages desktop` (`*.ts`, `*.tsx`) in gact-tui | — | — | static search | **pass** after CD-K3: the only match is `review_parts` in `gact/routes/system.py`, an unrelated substring; gact-tui has none |
| KQ36 | `scratchpad/kq36.py`: `gateway.build_gateway` over the saved `kicad` declaration from `.cloud-run/xdg/config/clio-agent/mcp.yaml` (pin `ae59c57`), then `Client(gw).list_tools()` and a call of `kicad_sim_run_operating_point` | — | — | mechanism (CLIO's own mount) | **pass**: 399 `kicad_*` tools, no `kicad_sim_*`; the call answers `ToolError: Unknown tool: 'kicad_sim_run_operating_point'` |

## Appendix: the person's messages

### Message 1 (15 Nov 2026)

> Changes to `plans/kicad-plugin-plan.md`. Record each one in the ledger as a decision that cites this message.
>
> **1. Fix a tool name.** In KV12 and the defaults, `get_bom_with_pricing` is wrong. The fork's tool is `lib_get_bom_with_pricing`.
>
> **2. Part specs (add to KT1).** Today the fork drops specs. `ComponentRecord` in `utils/component_search.py` has no specs field. `_extract_nexar_specs` keeps package, lifecycle, RoHS and the datasheet link and discards every other spec the Nexar query returns. `_record_from_digikey` reads `Parameters` only for the package. `JLCSearchClient` calls only `/api/search`.
>
> - `ComponentRecord` gains `specs`: every attribute the source returns, each with its name, value as shown, number, unit and source. Units are normalized, so 100nF and 0.1µF compare equal. This covers Nexar `specs`, DigiKey `Parameters`, Mouser's attributes, and the typed columns of jlcsearch's category endpoints (`/<category>/list.json`).
> - New tool `lib_get_component_specs(part, sources)`: one part's full spec sheet, merged across the configured sources, with the source on each value.
> - New tool `lib_search_parametric(category, filters, package, min_stock, sources)`: filters are `{name, op, value, unit}`. It maps to jlcsearch's category endpoints, and to Nexar's or DigiKey's filters when their keys are set.
> - New tool `lib_compare_parts(parts, attributes)`: parts side by side on the attributes given.
> - `lib_check_derating` reads ratings from `specs` instead of the description.
>
> **3. Datasheet values (add to KP1, `datasheet-pin-check`).** When a value the design needs is missing from the distributor specs (thermal resistance, absolute maximum ratings, a dropout curve), the agent takes it from the datasheet ingested into clio-core and records the page it came from. For a part with no KiCad symbol, the skill builds the symbol with the fork's `lib_generate_symbol_from_pintable` from the pin table it extracted.
>
> **4. A sixth view (add to KV2, KV4, KV5, KV8 and the views section).**
>
> - `ui://kicad/parts`, opened by `view_parts(query or category, filters, candidates)`. The agent can open it with its shortlist in `candidates`.
> - App-only data tool: `view_data_parts`.
> - The view is a spec table with filters, column choice and side-by-side compare. Operations: search (`lib_search_parametric`), compare (`lib_compare_parts`), and pick (`lib_bind_part_to_symbol` to the chosen symbol).
> - It reports the selected part and the compared parts as `selection` (KV7). App tool `select_row` as in the BOM.
> - The BOM view's rows also show the key specs from `specs`.
> - In the workbench (KW1), the parts view's selection joins `/pcb/selection`.
>
> **5. Qualification (add rows).**
>
> - KQ37: "find a 10 µF ≥ 25 V X7R 0805 with stock" with no keys. `lib_search_parametric` returns parts whose `specs` meet every filter, from jlcsearch.
> - KQ38: the same part with Nexar keys set. `lib_get_component_specs` merges both sources, and each value names its source.
> - KQ39: the agent opens the parts view with three LDO candidates. You compare two, pick one, and it is bound to the symbol.
> - KQ40: an IC whose distributor specs lack its thermal resistance. The agent's value comes from the ingested datasheet, with the page cited.
> - KQ41: a part with no KiCad symbol. The symbol is generated from the datasheet pin table, and the pin check passes.

### Message 2 (15 Nov 2026), on sourcing keys

> The fork queries part catalogs over the web: jlcsearch, an unofficial search over JLCPCB's catalog, needs no key. It's also what you'd use if JLCPCB assembles the board. Nexar (Octopart), DigiKey and Mouser work if you add their keys. This is going to test the connection semantic: does the user want to add the keys, where, how do we define that stuff dynamically etc.
>
> Okay, so on enable the MCP should pop up a window and ask, no? If then the primary search fails and it wants to fall back to below, it can ask elicitation, that would be ideal. Alternatively the MCP tool could respond with "hey, this tool failed because the search engine hit the limit, but you could set up access to a, b or c and ask the user for their key", then CLIO could set up some A2UI semantic to request the user. The key thing is that I really would prefer to change the source the least amount possible, so that we could possibly PR it to the main MCP, or at least continue to receive code updates on a regular basis.
>
> Real keys: No keys.

### Message 3 (15 Nov 2026), on installing KiCad

> Can CLIO install KiCad if you were to ask it? We did build a request-folders extension to the sandbox explicitly to be able to install and launch other programs like headless KiCad. What about asking for sudo, or any other password on other OSes?

### Message 4 (15 Nov 2026)

> You should build an install KiCad skill and include that mention, and specially if there is any parameter to skip it if running headless.

### Message 5 (15 Nov 2026)

> Trust the base code is not ours. Is the benefit of using someone else's code.
