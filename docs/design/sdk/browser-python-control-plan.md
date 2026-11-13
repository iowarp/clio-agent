# In-app browser with Python control: implementation plan

This plan covers item (g) of the CLIO 1.0 release: a browser the agent drives from Python inside `run_code`, shown live in the web UI as a tab the person and the agent share. It builds on the kernel from the programmatic tool calling plan and adds nothing beside it. It has five workstreams, and each builds on the ones before it:

1. **The browser service.** clio-agent owns one Chromium per profile through Playwright, on its own event loop thread. Pages belong to sessions and outlive the kernel, so a cancelled script or a restarted kernel never loses a tab.
2. **Browser tools.** Ten native tools in a new `browser` plugin. They run through the same instrumented path as every other tool, so the permission gate, the observer, nesting under `run_code`, cancellation and provenance all apply as they are. Navigation to a new site is a permission decision with grants per host.
3. **The `browser` kernel binding.** A small standard-library facade, shipped by the plugin, that gives scripts a Playwright-shaped `page` object. Locators are plain data built in the kernel; only actions and reads call tools. This adds the kernel's binding registry, with `tools` as its first entry.
4. **The shared tab in the UI.** A live view streamed over one WebSocket per page, with the person's input sent back to the same page, an agent cursor drawn by Playwright's action annotations, a URL bar and tabs per session. There are no locks: when the agent acts, the person stops touching.
5. **Comments on pages.** The element, region and text modes from the LaTeX and comments plan work on a live page, and the comment reaches the agent with a locator it can use directly.

The work runs to completion without stopping for review. The human reviews the finished branches.

## Contract

This plan is the contract for the five workstreams. The megachange design documents still govern everything they cover. Decisions D1 to D29 in `acceptance-ledger.md`, the F decisions of `delegation-fork-science-plan.md`, the M decisions of `long-term-memory-plan.md`, the model fleet decisions, the TD decisions of `tool-calling-plan.md` and the L decisions of `latex-and-document-comments-plan.md` stay in force. When code and this plan disagree, change the code. If the plan turns out wrong, choose the fix that best keeps its intent, update this document in the same change, and record the decision in the ledger.

| Document | Governs |
| --- | --- |
| This plan | The browser service, the browser tools and their permissions, the `browser` binding and the kernel's binding registry, the live view, comments on pages |
| `docs/design/sdk/browser-ledger.md` (new) | This plan's decisions and acceptance evidence |
| `docs/design/sdk/tool-calling-plan.md` | `run_code`, the kernel, dispatch, `callers` and default permissions, tool disclosure |
| `plugins-and-composition.md` | Plugins, the composed prompt, fragments |
| `agent-sdk.md` | `Agent`, `Plugin`, `native_tool`, `plugin check` |
| `latex-and-document-comments-plan.md` | The comment box, anchors and the HTML element picker that workstream 5 reuses |
| `runtime/sandbox.py` and `sandboxing-campaign-2026-07.md` | Confinement of agent-driven processes; this plan records why Chromium is not one of them |

**Base.** `develop` in clio-agent after `claude/tool-calling-long-horizon` has merged (read at that branch's `30c08f64`, gact-tui pin `b5256395`, marketplace `bd8b0d58`), with its submodule pins. `run_code`, `gact/code_mode/`, the `callers` and `permission` declarations and `parent_call_id` must be on the base; if they are not, stop and say so. Workstream 5 also needs the comment system from the LaTeX and comments plan (`claude/latex-document-comments`). If that has not merged when this work starts, build workstreams 1 to 4 on the PTC merge, rebase onto `develop` when the comment system lands, and run workstream 5 last.

Decisions use the prefix `BR`; qualification scenarios use `W`. These collide with no other plan.

## Rules

The megachange rules carry over unchanged: delete and replace, the task layer is the only execution authority, the model decides, no silent fallback, one mechanism per concept, no fifth store, the repository rules in `AGENTS.md`, and evidence over claims. The PTC plan's rules also hold, in particular one execution path for tools and a stable cached prefix. Five more apply here:

1. **The browser is reached through tools.** Every page action or read from a script is a call to a browser tool through `tools.*`, so it is gated, observed, nested and cancellable like any other call. The kernel never holds a Playwright client, a CDP endpoint, a WebSocket to the browser or a cookie. Playwright's own `browser.bind()` and `connect_over_cdp` are not offered to scripts.
2. **The kernel stays standard library only.** The `browser` binding is plain Python that builds tool calls. Playwright lives in the server.
3. **One owner for the browser.** `gact/browser/` owns Chromium, profiles, pages, the live view and input. Nothing else launches a browser for the agent. Test drivers in `scripts/cloud/` keep their own Playwright, since they act as the person, not as the agent.
4. **Sites are granted, not guessed.** Whether the agent may load a host is decided by the permission gate from the person's grants. No allowlist, reputation check or heuristic in code decides it.
5. **The person signs in.** The agent never types into a password field. Sign-ins happen in the shared tab by the person and persist in the profile.

## Out of scope

Do not implement, stub or reserve fields for these:

- Firefox and WebKit. Chromium only.
- Desktop control of native apps. That stays local-only, as decided, and is a separate item.
- A credential store, password manager integration, or filling passwords from secrets.
- Solving CAPTCHAs or evading bot detection.
- Recording sessions to video. Playwright's screencast can record, and the promo-video work may use it later, but this plan only streams frames.
- Browser extensions in the managed profile.
- Two people sharing one tab, or one tab shown in two CLIO desktops at once with both sending input. A second viewer sees the page but cannot send input (below).
- Direct (non-script) browser tools. Every browser tool is `callers="code"`; see the defaults table for how this is measured.
- Importing the installed Chrome's cookies or passwords into a CLIO profile: the operating system seals them (BR45). Attaching to the Chrome or Edge the person already runs, with their consent, is in scope since BR45 (the `cdp` backend below).

## Current state

What exists at the base, read on `claude/tool-calling-long-horizon` at `30c08f64`, so nothing is rebuilt and nothing is missed:

- **The kernel.** `gact/code_mode/kernels.py` starts one `python -I kernel_main.py` per `(session, react scope)` through `sandbox.wrap_confined` with `PROFILE_SHELL`, the shell's write roots and `net_policy=NET_ALLOW_RECORD`, from one long-lived spawner thread. A kernel is discarded on cancellation, timeout or a mode change, and replaced on the next script, so anything it held is gone.
- **Binding.** `kernel_main.py` answers one `bind` message by setting `namespace["tools"] = _Tools(host, stubs)`. The PTC plan describes "one binding registry whose only entry in this plan is `tools`", but the code has no registry: `tools` is a special case of `bind`. `_Tools` already has `search()` and `call(name, **args)`, and stubs exist for deferred tools.
- **Kernel cleanup.** `kernels.close_session` is documented as running when a session closes, but nothing calls it: the session delete route in `gact/routes/sessions.py` closes model clients, context files, tool output and ARC scopes, not kernels. A deleted session's kernel stops only at the idle reaper or process exit.
- **Dispatch.** `gact/code_mode/dispatch.py` runs each stub call on a worker thread in a copy of the `run_code` context through the agent's instrumented tool, never on the application event loop, and returns a native tool's return value as the structured value. `__search__` is the only special tool name. `ServerGates` caps concurrent calls per server.
- **Declarations.** `sdk.tools.native_tool(name, defer=, callers=, permission=)` lets a plugin claim a CLIO built-in tool with its defaults, as `plugins/science-log/ai.iowarp.clio/tools/builtin.py` does for the science-log tools, which clio-agent implements in `gact/science_log/tools.py` and registers through `BUILDERS` in `gact/agents/native_tool_registry.py`. `gact/agents/tool_settings.py` resolves `callers` and permission defaults, and a `code` tool falls back to `direct` with `tool_callers_code_unavailable` when `run_code` is absent.
- **Permissions by host.** `gact/runtime/permission_policies.py` has the `network_egress` request kind, whose `host` argument becomes a sticky `host_pattern` policy on `allow_workspace`, and `gact/runtime/grant_resolver.py` matches rows by `kind` (`tool`, `domain`, `fs_root`, `plan_acl`, `hook`) over one row shape. A `domain` row honours only workspace scope, as a guard for fleet egress.
- **Provenance.** `gact/artifacts/ingest_edges.py` records network egress as `used web:<domain>@<time>` edges.
- **Images to the model.** A script writes a file and views it with `view_image`; the pixels ride the `run_code` result to the model's next step (`gact/model_media.py`). The script itself gets only the image's facts.
- **Session scratch.** `runtime/session_scratch.session_scratch_dir(session_id)` is the per-session scratch folder, inside the readable roots.
- **Pinned downloads.** `runtime/ripgrep.py` and `runtime/github_cli.py` install pinned binaries through `runtime/package_preparation.py`, and `#1910` loads h5py only on the first HDF5 request. Neither pattern installs a browser.
- **WebSockets in GACT.** One route, `/v1/infrastructure/targets/{target_id}/transport` in `gact/routes/infrastructure.py`, admitted by `gact/infrastructure/transport_admission.py` with the `clio-bearer.` subprotocol from `gact/auth.py` and the origin guard.
- **No browser anywhere.** clio-agent has no Playwright dependency and no browser code; the only Chromium use is LibreOffice's `--headless` flag and the cloud test scripts. The marketplace `web` plugin is clio-kit's fetch and search server. gact-tui has no browser panel.
- **The picker.** The LaTeX and comments plan adds an element picker script that speaks `clio.html-preview.pick.start`, `pick.hover` and `pick.selected`, and says the in-app browser will reuse it.

## Design: the browser service

**Owner package.** `gact/browser/` with `service.py` (lifecycle, profiles, pages), `targets.py` (rebuilding a Playwright locator from a target spec), `tools.py` (the tool builders), `live.py` (the WebSocket and input), `install.py` (the browser download) and `presentation.py`, each under the 800-line cap.

**One loop.** Playwright's async API runs on one event loop thread owned by the service, started on the first browser call. A tool call arrives on a dispatch worker thread and submits its coroutine to that loop with `run_coroutine_threadsafe`, so a browser call never runs on the application event loop. Cancellation of the tool call cancels the coroutine, and every action has `browser.action_timeout_s` (default 30).

**Profiles.** The managed backend opens a persistent context per profile, `launch_persistent_context(user_data_dir=<state>/browser/profiles/<name>)`, so sign-ins survive restarts. `browser.profile` (default `default`) names it. One profile is one Chromium process; every session using it gets its own pages in that context, so a sign-in made in one session works in the next.

**Pages belong to sessions.** A page has an id (`p1`, `p2` within the server), an owning session and a label. Pages close when their session is deleted, from the delete path in `gact/routes/sessions.py`. That path also gains the `kernels.close_session(sid)` call it lacks today, so a deleted session's kernel stops then instead of at the idle reaper. A child session's pages close when its task settles; its last screenshot stays in its transcript. A fork starts with no pages, as it starts with a fresh kernel, and may open the parent's URL. At most `browser.max_pages` (default 12) are open per server; past that, `browser_open` returns `browser_page_limit`. When a profile has no pages for `browser.idle_minutes` (default 30), its browser closes.

**Backends.** `browser.backend` is `managed` (default) or `cdp`:

- `managed`: Playwright's Chromium in the new headless mode, launched by the service. It works the same on a laptop, a server and a remote agent, because the person sees it through the live view.
- `cdp`: `connect_over_cdp(browser.cdp_url, no_defaults=True)` to a Chrome or Edge the person runs, so Playwright does not change that browser's defaults. CLIO opens and touches only its own pages there. `browser.cdp_url` is an endpoint URL, or `chrome` / `msedge`: after the person allows remote debugging at `chrome://inspect/#remote-debugging` (Chrome 144 and later), CLIO reads the endpoint from `DevToolsActivePort` in that browser's user data folder, and Chrome asks the person on each attach (BR45, `gact/browser/chrome_discovery.py`). Chrome 136 and later refuse remote debugging on the default profile only by launch flag. Separately, `browser.channel = chrome / msedge` makes the `managed` backend run the installed browser on a profile CLIO keeps. The choice is made in **Settings > Browser** (`browser.backend`, `browser.channel`, `browser.cdp_url`; BR57), which replaced the planned "Use my Chrome" button, so nobody edits a configuration file. The live view works with both backends.

**Chromium's own sandbox.** Chromium is not started through `wrap_confined`. It has its own multi-process sandbox, and the fence ladder's backends (Codex's bubblewrap and Seatbelt, Landlock) were never built to host it; nesting it there is the unresolved question this plan settles by not doing it. The service launches Chromium with its sandbox on. When Chromium reports that its sandbox cannot start (running as root, no unprivileged user namespaces, a distribution policy that restricts them), the service refuses with `browser_sandbox_unavailable` and the reason, unless `browser.allow_unsandboxed` is true; then it launches with `--no-sandbox`, the trace and doctor say so, and the live view shows a warning icon with the reason on hover. The cloud stack sets the flag because it runs as root. Phase 0 records what happens on a stock Ubuntu 24.04 desktop, macOS, Windows and one HPC login node, and the doctor row reports it everywhere.

**Network.** The browser's own requests are not the kernel's, so they do not pass the kernel's fence. Top-level navigation is gated per host (below). When the deployment runs deny-mode egress through CLIO's proxy, the service launches Chromium with that proxy, so subresource requests meet the same chokepoint and the same `used web:` records; otherwise subresources load as in any browser. Phase 0 confirms how `runtime/sandbox_net.py` exposes the proxy.

**Files.** Downloads land in the owning session's scratch folder and are reported in the result of the call that triggered them. Uploads (`set_input_files`) take paths that pass the same file policy check as `fs_read_file`. Screenshots are written to session scratch.

**Install.** The Python `playwright` package (at least 1.64, for `page.screencast`, `aria_snapshot` in `ai` mode and `get_by_ref`) is a clio-agent dependency pinned in `uv.lock` and imported only when the service starts, as h5py is. The Chromium build matching that Playwright release is downloaded on first use into CLIO's cache root through `package_preparation`, never at install time, so the base install and the desktop bundle do not grow. CLIO does not run `playwright install-deps`, which needs root; missing system libraries produce `browser_runtime_missing_libraries` with the list Chromium printed. Doctor reports the version, the cache path and the sandbox state.

## Design: browser tools

**The plugin.** A new marketplace plugin `browser` claims ten CLIO built-in tools implemented in `gact/browser/tools.py`, registered through `BUILDERS` in `native_tool_registry.py`. Each is declared `callers="code"`, so scripts reach them through the binding and their schemas never enter the prompt. The plugin ships the binding (below), a skill `use-the-browser` and a fragment `browser.use` (about 80 words, `when: {tools: [browser_open]}`). It joins the standard agent's composition, since agents are complete by default.

**Targets.** Element-addressing tools take a `target`, a JSON description of a Playwright locator that `targets.py` rebuilds on the host:

```json
{"steps": [
  {"by": "role", "role": "row", "name": "HDF5", "exact": false},
  {"by": "filter", "has_text": "2026"},
  {"by": "nth", "index": 0}
], "frame": [{"by": "css", "css": "iframe#results"}]}
```

`by` is one of `ref`, `role`, `text`, `label`, `placeholder`, `alt`, `title`, `test_id`, `css`, `filter`, `nth`, `first`, `last`. `ref` is an aria ref such as `e14` from the latest snapshot of that page, resolved with `page.get_by_ref`. Playwright's strictness applies: an action on a target that matches several elements fails with the count, so the script narrows it.

**The tools.** Every result carries the page (`id`, `url`, `title`, `host`), `person_acted` (true when the person navigated or sent input to this page since the agent's previous call on it, so the agent knows the page may have changed under it) and any downloads that call produced.

| Tool | Arguments | Effect |
| --- | --- | --- |
| `browser_open` | `url=None`, `label=""` | Opens a page for the session, navigating when a URL is given; returns its id |
| `browser_pages` | | Lists the session's pages |
| `browser_close` | `page` | Closes one page |
| `browser_navigate` | `page`, `url=None`, `action="goto"` (`goto`, `back`, `forward`, `reload`), `wait_until="load"` | Navigates |
| `browser_snapshot` | `page`, `target=None`, `boxes=False` | The ARIA snapshot in `ai` mode, with refs, as text |
| `browser_read` | `page`, `target`, `what` (`text`, `inner_html`, `value`, `attribute`, `count`, `box`, `visible`, `checked`), `name=None` | Reads from the page |
| `browser_act` | `page`, `target=None`, `action`, `value=None`, `x=None`, `y=None`, `options={}` | `click`, `dblclick`, `hover`, `fill`, `type`, `press`, `select`, `check`, `uncheck`, `upload`, `drag_to`, `scroll`, `mouse_click` and `key` (the last two by coordinates or for the focused element) |
| `browser_wait` | `page`, `until` (`load`, `url`, `target`, `gone`, `idle`), `value=None`, `target=None`, `timeout_s=None` | Waits for a state |
| `browser_screenshot` | `page`, `target=None`, `full_page=False` | Writes a PNG to session scratch and returns its path and size |
| `browser_evaluate` | `page`, `expression`, `arg=None` | Runs JavaScript in the page and returns its JSON value |

**Annotations and defaults.** `browser_pages`, `browser_snapshot`, `browser_read`, `browser_wait` and `browser_screenshot` are read-only. `browser_open`, `browser_navigate`, `browser_act` and `browser_close` are not. `browser_evaluate` is declared `permission="ask"`, because a page script runs with the page's origin and the person's sign-ins; a person's rule can change it, as with any tool. Everything else follows the approval mode and the script's grant as the PTC plan defines.

**Hosts are granted.** Loading a host the session has not been granted is a permission request of a new kind, `browser_host`, carrying the host. It goes through the one interactive gate, offering allow once, for this conversation, for this workspace, or deny. A grant is a policy row of `kind="browser_host"` with `host_pattern`, a new discriminator over the existing row shape in `grant_resolver.py`, honouring session and workspace scope. It is not the egress `domain` kind: letting a sandboxed process reach a host and letting the agent browse a site with the person's sign-ins are different grants. The check runs in one place, request interception (DevTools `Fetch` at request stage for the main frame's `Document` requests) on each page, which sees every main-frame navigation request and every redirect hop, so a link the agent clicks, a redirect and a form post are all covered, not only `browser_navigate` (BR20: Playwright's route handler misses redirect hops). While the question is open the navigation is held; denial aborts it and raises in the script. A host the person opens in the shared tab is granted for that conversation, recorded as their grant. Subframes and subresources are not gated by host; they belong to the page the person or the agent chose. `browser.granted_hosts` in the configuration can pre-grant hosts for a deployment.

**Passwords.** `fill` and `type` into a password field are refused with `browser_password_field`. The result tells the agent to ask the person to sign in in the shared tab.

**Provenance and evidence.** Every completed navigation records a `used web:<host>@<time>` edge through `ingest_edges.py`, so the science log's sources and the Evidence view see browsed pages as they see fetched ones. Screenshots are session artifacts like any file the agent writes.

**Concurrency.** The browser tools form one group in `ServerGates`. Calls on different pages run concurrently; calls on one page are serialized by the service, in the order they arrive.

## Design: the `browser` kernel binding

**The registry.** The `bind` message becomes `{"type": "bind", "stubs": {...}, "bindings": {"browser": {"source": "...", "digest": "..."}}}`. The kernel builds `tools` from the stubs as today, then, for each binding, creates a module, gives it `tools` and `ToolError`, executes its source, and binds the module's `export` object under the binding's name. `Kernel.bind` sends a binding only when its digest changed. A plugin declares bindings in its extension, `"ai.iowarp.clio": {"kernel_bindings": {"browser": {"source": "ai.iowarp.clio/kernel/browser.py", "requires": ["browser_open", "browser_act"]}}}`; a binding is bound only when every tool it requires is callable from a script for that agent, so a denied or removed tool removes the binding with it. `plugin check` compiles each binding and rejects any import outside `sys.stdlib_module_names`. The special-cased `tools` assignment is replaced by the registry, with `tools` as its first, generated entry.

**The facade.** `ai.iowarp.clio/kernel/browser.py` (BR21) exports `browser` with Playwright's names, all async, so what a model knows about Playwright carries over:

```python
page = await browser.open("https://arxiv.org/list/cs.DC/new")
print(await page.snapshot())                    # ARIA tree with refs
entries = page.get_by_role("listitem").filter(has_text="HDF5")
for i in range(await entries.count()):
    print(await entries.nth(i).inner_text())
await page.get_by_label("Search term").fill("parallel I/O")
await page.keyboard.press("Enter")
await page.wait_for_load_state()
shot = await page.screenshot(full_page=True)    # a path; view it with view_image
```

**Locators are data.** `get_by_role`, `get_by_text`, `get_by_label`, `get_by_placeholder`, `get_by_alt_text`, `get_by_title`, `get_by_test_id`, `get_by_ref`, `locator`, `frame_locator`, `filter`, `nth`, `first` and `last` return new locator objects that only append a step. Nothing calls a tool until a terminal method: `click`, `fill`, `press`, `inner_text`, `count`, `is_visible`, `wait_for` and the rest map one-to-one onto `browser_act`, `browser_read` and `browser_wait`. `page.goto`, `go_back`, `reload`, `snapshot`, `screenshot`, `evaluate`, `wait_for_url`, `wait_for_load_state`, `keyboard` and `mouse` map onto the page-level tools. `browser.pages()` lists the session's pages, and `browser.page("p2")` returns one by id, so a page opened in an earlier script, or by the person, is reachable after the kernel restarts.

**Where it differs from Playwright,** written in the facade's module docstring so `help(browser)` explains it: every method is async; there are no element handles, `page.on(...)` events or routes (use `wait_for_*`); `screenshot` returns a path rather than bytes; `evaluate` takes and returns JSON values only; errors are `ToolError` carrying the tool's typed reason.

## Design: the shared tab

**The live view.** `GET /v1/sessions/{sid}/browser/{page_id}/live` upgrades to a WebSocket, admitted like the transport route: the `clio-bearer.` subprotocol and the origin guard. The server streams JPEG frames from `page.screencast.start(on_frame=...)` as binary messages and page state as JSON (`url`, `title`, `loading`, `agent_acting`, the agent's last action). The screencast runs only while at least one viewer is connected. Frames go through GACT like everything else, so the view works the same for a local agent, a remote one and one behind clio-relay's tunnel, and the UI stays independent of where the agent runs.

**Input.** The view sends `mouse` (move, down, up, click with button and count, in page CSS pixels), `wheel`, `key` (down and up), `text`, `resize` and `navigate` (the URL bar, back, forward, reload) messages, which the service plays through `page.mouse`, `page.keyboard` and the same navigation path as `browser_navigate`. Person-sourced input sets `person_acted` for the agent's next call. There are no locks and no control modes: both can act at any time. The first connected viewer of a page sends input; others see the page with a "view only" icon.

**The agent cursor.** The service turns on `page.screencast.show_actions` with a CSS style from the active theme, so each agent action shows a pointer at its target, a highlight on the element and a short title. Playwright draws these into the page, so they appear in the live view and in the real window of the `cdp` backend alike. Phase 0 confirms the second; if annotations reach only screencast frames, the panel draws the pointer from the action events instead, and the `cdp` window goes without it.

**Latency.** The view must feel direct for clicking and typing on a page. Phase 0 measures input-to-frame time on a local stack and through the remote tunnel, records it, and sets the frame size and quality from it. A 3D canvas on a web page will feel like a video stream; that is accepted for this plan.

**Where it appears.** A Browser surface in the workspace pane beside documents, with a tab per page labelled by its session (a subagent's page by its task), the URL bar, back, forward and reload, and status as icons with detail on hover: loading, agent acting, view only, sandbox off. The Work rail lists open pages under their session. At 390 px wide the surface becomes a full-screen sheet, the frame scales to fit, and a tap is sent as a click at the mapped point.

**In the transcript.** Browser calls are nested under their `run_code` row through `parent_call_id`, each presented as its action, a short form of the target (role and name, or the ref) and the host. A screenshot call shows the image. A snapshot is collapsed to its line count with the text on expand. A `browser_host` question shows the host and the script it came from.

## Design: comments on pages

**Modes.** The comment control in the Browser surface offers the three modes from the LaTeX and comments plan. Element mode injects that plan's picker script into the page with `add_init_script` and reports `pick.hover` and `pick.selected` back through `expose_binding`; the panel forwards pointer positions while picking and draws the hover outline from the frames. Region mode drags a rectangle on the frame. Text mode selects text in the page through the picker script's selection report.

**What the agent receives.** A page is live, not a versioned artifact, so a page comment is an attachment to the next message, as chat annotations are in the comment plan, not a stored review. It uses the same comment box and the same renderer. Each entry carries the page id and URL, the mode, the selected text or element (tag, a truncated `outerHTML` excerpt, the box), the comment, a cropped screenshot of the area as an image part, and a target spec for the element built from its role and accessible name (falling back to the picker's selector), so the agent's script can act on it with `page.locator(...)` or the role locator without searching for it:

```
Comment on page p2, https://example.org/settings (element)
  Element:  <button>, role button "Save changes"
  Target:   {"steps": [{"by": "role", "role": "button", "name": "Save changes"}]}
  Text:     "Save changes"
  Box:      132 x 36 px at (980, 640) in a 1280 px wide view (attached as image 1)
  Comment:  This is disabled after I edit the email field. Find out why.
```

## Defaults for the open questions

Take these and record each in the ledger.

| Question | Default |
| --- | --- |
| Backend | `managed`; `cdp` by setting |
| Headless mode | Chromium's new headless mode, not the headless shell |
| Profiles | One profile, `default`, per CLIO user, shared by sessions |
| Pages of a fork | None; the fork may open the parent's URL |
| Pages of a child | Close when its task settles |
| Callers | `code` for every browser tool; the evaluation below decides whether `browser_snapshot` should also be `direct` |
| Host grants | `browser_host` kind; asked per new host; a host the person opens is granted for the conversation |
| Frames and subresources | Not gated by host |
| `browser_evaluate` | Default permission `ask` |
| Password fields | Refused; the person signs in |
| Unsandboxed Chromium | Refused unless `browser.allow_unsandboxed`; the cloud stack sets it |
| Several viewers | The first sends input; the rest view only |
| Limits | 12 pages per server, 30 s per action, 30 minutes idle before a profile's browser closes |

## Phase 0: inventory and decisions

No production code changes in this phase.

1. Pin the Playwright release (at least 1.64) and its Chromium build. On the cloud host, confirm `page.screencast.start(on_frame=...)`, `show_actions(style=...)`, `aria_snapshot` in `ai` mode with `boxes`, `get_by_ref`, `launch_persistent_context` and `connect_over_cdp(no_defaults=True)` work as this plan uses them, and record any signature that differs.
2. Check whether `show_actions` annotations are drawn into the page (and so a real `cdp` window) or only into screencast frames, and record the agent-cursor decision.
3. Measure input-to-frame latency for a click and for typed text on a local stack and through the desktop tunnel, at two frame sizes, and set the defaults.
4. Record Chromium's sandbox result on the cloud host (as root), and leave rows for a stock Ubuntu 24.04 desktop, macOS, Windows and one HPC login node for the local phase.
5. Confirm how `runtime/sandbox_net.py` exposes CLIO's egress proxy and whether a deny-mode deployment can hand it to Chromium.
6. Confirm the main-frame route handler sees link clicks, redirects and form posts, and can hold a navigation while a permission question is open.
7. List every caller of the kernel's `bind` and `namespace["tools"]`, for the registry change.
8. Build the fixture site under `tests/browser/fixtures/site/`, served by `scripts/cloud/stack.sh` at two hosts, `a.fixture.localhost` and `b.fixture.localhost`, which Chromium resolves to loopback: a sign-in form with a password field, a paginated table, a form with a select, checkboxes and a file input, a link and a redirect to the other host, an iframe, a download, a slow page, and a script-rendered list.
9. Write `docs/design/sdk/browser-ledger.md` with the megachange ledger's columns, decisions BR1 onward and the scenarios below. Commit, push, open draft PRs, and continue.

## Phase 1: the browser service

1. `gact/browser/service.py`: the loop thread, profiles, persistent contexts, pages owned by sessions, page limits, the idle reaper, child and fork rules.
2. Session deletion closes the session's pages and calls `kernels.close_session`, with a test that deletes a session holding a kernel and a page and finds neither process left.
3. `install.py`: the lazy Playwright import, the first-use Chromium download through `package_preparation`, the doctor row, `browser_runtime_missing_libraries`.
4. The sandbox check, `browser_sandbox_unavailable`, `browser.allow_unsandboxed`, and the cloud stack's setting.
5. The `cdp` backend and its connection errors as typed reasons.
6. Every `browser.*` key through `conf` with its key note and the regenerated environment reference.

## Phase 2: browser tools and grants

1. `targets.py` and its tests against the fixture site, including strictness failures and frames.
2. The ten tools, their annotations, results with `person_acted` and downloads, uploads through the file policy, screenshots to scratch, and the password refusal.
3. The `browser_host` request kind and policy kind, the main-frame route handler holding navigations for the gate, grants from the person's own navigation, `browser.granted_hosts`.
4. `used web:` edges for navigations.
5. The marketplace `browser` plugin: `native_tool` claims with `callers="code"` and `browser_evaluate`'s `permission="ask"`, the skill, the fragment, and its place in the standard agent.

## Phase 3: the binding

1. The binding registry in `kernel_main.py` and `Kernel.bind`, with `tools` as its first entry; the special case is deleted.
2. `kernel_bindings` in the plugin extension, resolution against the agent's script-callable tools, and the `plugin check` import rule.
3. `ai.iowarp.clio/kernel/browser.py`, its docstring, and unit tests that run it in a real kernel against a recording `tools` stub.

## Phase 4: the shared tab

1. `live.py`: the WebSocket route and admission, the screencast while viewers are connected, input playback, view-only viewers.
2. The agent cursor from Phase 0's decision.
3. The Browser surface, tabs, URL bar, status icons and the rail entry in gact-tui, and the phone-width sheet.
4. The transcript presentation of browser calls and of the `browser_host` question.

Verify each UI change as the megachange plan describes: restart the stack, run `node scripts/cloud/ui_check.mjs` with a `--steps` script at 1440 by 900 and 390 by 844, read every screenshot, and check that `consoleErrors` and `failedRequests` are empty.

## Phase 5: comments on pages

1. Inject the comment plan's picker into managed pages and report through `expose_binding`.
2. Element, region and text modes in the Browser surface, through the shared comment box.
3. The page-comment attachment and its rendering, with the crop as an image part and the target spec.

## Phase 6: qualification

Run focused tests and `ruff` on touched paths while working. Then run these against a freshly restarted stack (`claude_code` provider), and record each in the ledger with commands, handles, numbers and the screenshots read. Every request is a plain request that names no tool.

| ID | Scenario | Passes when |
| --- | --- | --- |
| W1 | Fresh install, then "open the fixture table and give me every row" | Chromium downloads on first use; the agent pages through the table in one `run_code` script; browser calls appear nested under it; the answer matches the fixture's rows |
| W2 | The first navigation to `a.fixture.localhost` | One `browser_host` question naming the host and showing the script; "for this conversation" lets later scripts load it without asking |
| W3 | The agent follows a link that redirects to `b.fixture.localhost` | The navigation is held for a new question; denying it raises in the script and the page stays on host `a` |
| W4 | A task that needs the sign-in page | The agent asks the person to sign in; a `fill` into the password field returns `browser_password_field`; the person signs in through the live view; the agent continues on the signed-in page |
| W5 | Restart the server after W4, then a new conversation uses the same site | The profile still holds the sign-in |
| W6 | Fill the fixture form, pick a select option, tick two boxes, upload a workspace file, submit | The fixture records the submitted values; the upload outside readable roots is refused by the file policy |
| W7 | Cancel a turn in the middle of a long script driving a page | The kernel is replaced, the page is still open with its state, and the next script reaches it through `browser.page(...)` |
| W8 | Two `explore` children each browse a different fixture page | Each has its own page labelled by its task in the Browser surface; both close when their tasks settle |
| W9 | The person clicks in the live view while the agent's script is waiting | The agent's next result has `person_acted: true`, and the agent re-reads the page before acting |
| W10 | Open the page in a second browser window of the UI | It shows the frames with the view-only icon and its input is ignored |
| W11 | A download link on the fixture | The file lands in the session's scratch folder and the triggering call reports it |
| W12 | A script calls `page.evaluate(...)` in `auto-edits` | It asks, because of the plugin's default permission, though the script is approved |
| W13 | An element comment on a fixture button, sent with the next message | The agent receives the rendered entry with the crop as an image part and the target spec, and its script acts on that exact element |
| W14 | A region comment over the fixture table | The crop arrives as an image part and the agent answers about that area |
| W15 | "What was posted in cs.DC today that mentions HDF5?" on arxiv.org | The agent asks for the host, reads the listing in a script, and its answer cites entries that are on the page; the session's provenance has a `used web:arxiv.org` edge |
| W16 | The live view at 390 by 844 | The sheet opens full screen, the frame fits, and a tap clicks the mapped element |
| W17 | Export a W1 session | The export shows the `run_code` cell, its nested browser calls and the screenshots offline |
| W18 | `plugin check` on a binding that imports `requests` | It is rejected, naming the import |
| W19 | Search the repositories for a second browser launch outside `gact/browser/` and the test drivers | None |

Evaluation, recorded rather than passed: on W1, W6 and W15, steps, tokens and wall time with the browser tools as `code` and again with `browser_snapshot` also `direct`. The numbers decide whether to keep the default.

The cloud session cannot qualify the Tauri desktop, branded Chrome and its attach consent prompt (the channel and attach mechanisms passed live against Chrome for Testing, BR54), Windows, macOS or HPC login nodes; those rows are recorded as local phase. Nothing else may be blocked. A failing scenario is fixed, not deferred.

## Done

1. Every phase is implemented, and the kernel's special-cased `tools` bind is gone.
2. Focused tests pass, and `ruff check` and `ruff format --check` are clean on touched paths.
3. Every cloud-phase scenario passes against a freshly restarted stack, each shown with commands, handles, numbers and the screenshots read; local-phase rows say so.
4. The ledger holds every decision with its evidence, the Phase 0 measurements and the evaluation numbers.
5. The branches are pushed in clio-agent, gact-tui and clio-agent-marketplace, the submodule pins point at the pushed commits, and the draft PRs carry a summary and the evidence.

## Working conventions

- Branch `claude/browser-python-control` in clio-agent, gact-tui and clio-agent-marketplace. Open draft PRs against `develop`.
- Small Conventional Commits per step, with test evidence in each PR description.
- Keep runtime state in `.cloud-run/`. Stop the stack when you finish a work block.
