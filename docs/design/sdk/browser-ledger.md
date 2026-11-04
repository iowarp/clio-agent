# In-app browser ledger: the browser service, tools, binding and shared tab

Rows follow [browser-python-control-plan.md](browser-python-control-plan.md) as amended by
[browser-python-control-addendum-a.md](browser-python-control-addendum-a.md). Status values are
the ones [acceptance-ledger.md](acceptance-ledger.md) defines (`passed (live)`, `tests only`,
`static`, `done`, `open`, `blocked`), plus `local phase` for a row the plan assigns to the local
qualification pass and `pending` for a row waiting on another plan's work.

Decisions use the prefix `BR`, qualification scenarios `W`.

**Branch.** `claude/gracious-goldberg-gagoo4` in clio-agent, gact-tui and
clio-agent-marketplace (addendum A, "Branch"). **Base.** The head of
`claude/tool-calling-long-horizon`: clio-agent `be8b90bb`, gact-tui `b5256395`, marketplace
`bd8b0d58` (addendum A, "Base"); rebased onto `develop` when that branch merges.

## Phase 0 measurements

Cloud host, as root, Playwright 1.64.1 (Python), Chromium `chromium-1248` (156.0.8078.4),
downloaded by Playwright's own installer through the session proxy in about 30 s (120 MiB, plus
the headless shell and ffmpeg it fetches alongside). Probes:
[`scripts/cloud/browser_probe/`](../../../scripts/cloud/browser_probe/), against the fixture site
[`tests/browser/fixtures/site/server.py`](../../../tests/browser/fixtures/site/server.py) on port
8790.

| Item | Result |
| --- | --- |
| API on 1.64.1 | Present as the plan uses them: `Screencast.start(on_frame, quality, size)` and `Screencast.show_actions(cursor, duration, font_size, position, style={point, highlight, title})`, both returning async context managers; `Locator.aria_snapshot(mode="ai", boxes, depth)`; `Page.get_by_ref(ref)`; `launch_persistent_context`; `connect_over_cdp(no_defaults=)`. |
| Sandbox as root | `chromium_sandbox=True` fails: "Chromium sandboxing failed! ... (preferred): Configure your environment to support sandboxing". The service matches this message for `browser_sandbox_unavailable`. |
| Headless mode | `channel="chromium"` runs the full `chrome-linux64/chrome` build (new headless), not the headless shell; its user agent still says `HeadlessChrome`. |
| Route handler (`context.route`) | Sees link clicks and form posts as main-frame navigation requests and can hold one (3.03 s measured). **Does not see redirect hops**: a 302 to the other host reached it only as the first request, and a "denied" redirect still landed on host b. `route.fetch(max_redirects=0)` cannot replace it: it runs in Playwright's driver, which does not resolve `*.localhost`, and it would be a second network client. |
| DevTools `Fetch` at request stage, `resourceType: Document` | Pauses every main-frame hop, redirects included (`redirectedRequestId` set on the hop to b), and the timed redirect (a hop 5.0 s after load). `Fetch.failRequest(errorReason="Aborted")` keeps the page on the page it was on, after both a click (`/link-to-b` stays) and a `goto` (which raises `net::ERR_ABORTED`). `Abort` through `route.abort("blockedbyclient")` instead left a click on `chrome-error://chromewebdata/`. |
| Cookies and persistence | The sign-in's 303 sets the cookie through the gated path; after closing and relaunching the persistent context on the same profile, `/account` is still signed in. |
| Snapshot refs | `aria_snapshot(mode="ai")` refs are frame-scoped (`f9e1`, `f9e2`, ...); `boxes=True` adds `[box=x,y,w,h]`; `get_by_ref("f9eN")` clicks the snapshot's Next link. |
| Strictness | `get_by_role("row").click()` fails with "strict mode violation: get_by_role("row") resolved to 11 elements". |
| Frames | `frame_locator("iframe#results").get_by_role("button").click()` works. |
| Screencast | Frames at `quality=70, size=960x600` on a 1280x800 viewport: 36 frames in about 2 s of activity, median 12.9 KB; `viewportWidth` reports the page's 1280. First frame 65 ms after a click and 22 ms after typed text, locally (no tunnel). |
| Agent cursor | A page screenshot taken during `show_actions` shows Playwright's pointer drawn into the page, so annotations reach a real `cdp` window as well as the screencast (plan item 2): the agent cursor is Playwright's in both backends. |
| Downloads | `expect_download` on the fixture's attachment link returns `report.csv` with its URL; `save_as` writes it. |
| `cdp` backend | `connect_over_cdp("http://127.0.0.1:9333", no_defaults=True)` to a separately started Chromium: one existing context, a new page navigates, `show_actions` and `screencast.start` work. |
| Through the desktop tunnel | Not measurable from a cloud session; local phase. |

## Seams found (Phase 0 items 5 to 7 and addendum A Phase 0 items 2 and 3)

| Item | Finding |
| --- | --- |
| Egress proxy | `runtime/sandbox_net.open_child_egress` serves only confined children (`sandbox.py:494`). The lower layer, `net_chokepoint.open_child_channel(child_id, mechanism=, workspace_root=)`, returns a loopback port any process can use; Chromium takes it as `--proxy-server` with `--proxy-bypass-list=<-loopback>` (Chromium bypasses loopback by default). The deny-mode gate attributes a connection by the channel's workspace root. |
| `bind` callers | `kernel_main.main` (`namespace["tools"] = _Tools(...)`) and `kernels.Kernel.bind`, called only from `code_mode/tool.run_script`. Nothing else reads `namespace["tools"]`. |
| Declaration seam for `used` URL edges | There is no web-fetch-specific seam. `transform_edges.detect_authority_edges` reads a tool's result and returns `used` edges whose `authority` is a URL (NDP `stage_resource`, relay fetch); `ingest_edges.declare_web_domains` (step 0) then keys any such http(s) edge as `web:<host>` with `net_mechanism=tool-declared`. Browser files declare their page URL there. |
| Minting browser files | Files written by a native tool are not minted by the observer; `capture_a2ui_surface` mints its PNG with `artifacts.minting.mint_artifact` and `storage.ingest_artifact_identity`. Screenshots and downloads are minted the same way. |
| Navigation attribution | A main-frame hop is the agent's while one of its browser calls on that page is in flight, including that call's `wait_until` window; the person's while a person input or URL-bar navigation on that page is less than `browser.person_window_s` (2 s) old; otherwise the page's own. Measured inputs: the timed redirect fired 5.0 s after load with no call in flight. |

## Decisions

| ID | Decision | Source | Evidence | Status |
| --- | --- | --- | --- | --- |
| BR1 | Work on the head of `claude/tool-calling-long-horizon` in all three repositories, on the session branch; rebase onto `develop` when that merges. Workstream 5 runs after the comment system lands; W13 and W14 are `pending` until then. | Addendum A, "Base and branch" | This ledger's header | done |
| BR2 | The branch is `claude/gracious-goldberg-gagoo4` in all three repositories, in place of `claude/browser-python-control`. | Addendum A, "Branch" | This ledger's header | done |
| BR3 | Backend `managed` by default; `cdp` by setting, with `no_defaults=True`. | Plan defaults | Phase 0 `cdp` row | open |
| BR4 | Chromium's new headless mode (`channel="chromium"`, the full build), not the headless shell. | Plan defaults | Phase 0 headless row | open |
| BR5 | One profile, `default`, per CLIO user, shared by sessions; sign-ins persist in it. | Plan defaults | Phase 0 persistence row | open |
| BR6 | A fork starts with no pages; a child's pages close when its task settles. | Plan defaults | | open |
| BR7 | Every browser tool is `callers="code"`; the W1, W6 and W15 evaluation decides whether `browser_snapshot` is also `direct`. | Plan defaults | | open |
| BR8 | Hosts are granted with a `browser_host` request kind and policy kind, asked per new host; a host the person opens is granted for the conversation; frames and subresources are not gated. | Plan defaults | | open |
| BR9 | `browser_evaluate` has default permission `ask`. | Plan defaults | | open |
| BR10 | `fill` and `type` into a password field are refused with `browser_password_field`; the person signs in. | Plan defaults | | open |
| BR11 | Unsandboxed Chromium is refused with `browser_sandbox_unavailable` unless `browser.allow_unsandboxed`; the cloud stack sets it because it runs as root. | Plan defaults | Phase 0 sandbox row | open |
| BR12 | The first viewer of a page sends input; others view only. | Plan defaults | | open |
| BR13 | 12 pages per server, 30 s per action, 30 minutes idle before a profile's browser closes. | Plan defaults | | open |
| BR14 | Pin `playwright==1.64.1`. `show_actions(style=...)` and `get_by_ref` exist on it, so neither fallback of addendum A item 1 applies: annotations are themed and `ref` stays in the target vocabulary. | Addendum A item 1 | Phase 0 API row | done |
| BR15 | Provenance comes from the call record: each browser call is recorded with its page URL; a file a browser tool produces (screenshot, download) declares the page URL as a `used` edge through `detect_authority_edges`, keyed `web:<host>` and `tool-declared` by `declare_web_domains`. No navigation edges and no per-navigation egress records. W15 checks the sources and the screenshot's edge. | Addendum A item 2 | Seams table | open |
| BR16 | Rule 3 and W19 exempt `scripts/cloud/`, `scripts/live_verification/`, `site/e2e/` and `tests/`; W19 passes when nothing under `src/` outside `gact/browser/` launches or connects to a browser. | Addendum A item 3 | | open |
| BR17 | Waiting for the person's answer to a `browser_host` question does not count against `browser.action_timeout_s`. The question goes through `run_code`'s `script_grants.question()`, so the same mechanism pauses both clocks; Stop cancels the question and the action. Playwright actions run with `timeout=0` and the service enforces the action clock itself, since Playwright's clock cannot pause. | Addendum A item 4 | | open |
| BR18 | A main-frame navigation is attributed to an agent call in flight on that page (asked with the call and its script), else to the person (granted for the conversation), else to the page itself (refused unless granted; the panel offers to allow it; the next result carries `navigation_blocked`). | Addendum A item 5 | Seams table, attribution row | open |
| BR19 | The `browser.use` fragment is gated `when: {tools: [run_code, browser_open]}`. The addendum's check command, `clio-agent plugin resolve <agent> --exact`, does not exist; the exact prompt is `GET /v1/sessions/{sid}/agent?exact=1` (`gact/routes/plugins.py`), which W1 reads. | Addendum A item 6; Phase 0 | | open |
| BR20 | Host gating uses DevTools `Fetch` at request stage for `Document` requests of the page's main frame, through a CDP session per page, not `context.route`: the route handler misses redirect hops (Phase 0). A denied hop fails with `Aborted`, which keeps the page where it was. The plan's sentence "a route handler on the page that sees every main-frame navigation request" is amended to this mechanism. | Phase 0 route and Fetch rows | | open |
| BR21 | A plugin's kernel binding source lives under `ai.iowarp.clio/` (`ai.iowarp.clio/kernel/browser.py`), not at `kernel/browser.py`, so the plugin's trust digest counts it as executable code and a changed binding needs the plugin trusted again. It is read as text and executed only in the kernel. | Phase 0; `plugins/loader.py` `_EXECUTABLE_GLOBS` | | open |
| BR22 | Files at the 800-line cap do not grow: `GrantRecord` moves from `grant_resolver.py` to `grant_record.py`; the health report takes the browser row through a helper in `package_preparation`; baselined files (`gact/app.py`, `gact/routes/sessions.py`, `runtime/status.py`) gain one call each with their baseline raised in the same commit; in gact-tui, the workbench's open-request switch moves to its own module. | Cleanup program, no accretion | | open |
| BR23 | Kernels and browsers are stopped by an explicit call in the server's shutdown, not only by `atexit`: a managed shutdown ends in `os._exit(0)`, which skips `atexit`. Session deletion stops the session's kernels and pages through one owner function. | Phase 0 (`desktop_lifecycle.terminate_process_after_cleanup`) | | open |
| BR24 | In a deny-mode workspace, Chromium is launched with an egress channel from `net_chokepoint.open_child_channel` keyed to the workspace of the session that started the profile's browser. A profile used by sessions of two workspaces at once attributes its subresource egress to the first; this is accepted and recorded, since the profile is shared by design (BR5). | Phase 0 egress row | | open |
| BR25 | Draft PRs target `claude/tool-calling-long-horizon` until it merges, then `develop`; against `develop` now they would carry that branch's 300 and more commits. | Addendum A, "Base" | | open |

## Qualification

| ID | Scenario | Evidence | Status |
| --- | --- | --- | --- |
| W1 | Fresh install, the fixture table in one script, nested calls, the exact prompt has `browser.use` for the standard agent and not without `run_code` | | open |
| W2 | First navigation to host a asks once; "for this conversation" covers later scripts; an answer after 45 s still continues | | open |
| W3 | A redirect to host b is held for a new question; denying it raises and the page stays on host a | | open |
| W4 | Sign-in: the agent asks the person; `fill` on the password field returns `browser_password_field`; the person signs in through the live view | | open |
| W5 | Server restart, new conversation: the profile still holds the sign-in | | open |
| W6 | Form: select, two boxes, workspace upload, submit; an upload outside readable roots is refused | | open |
| W7 | Cancel mid-script: the kernel is replaced, the page stays, `browser.page(...)` reaches it | | open |
| W8 | Two `explore` children, each with its own labelled page, closed when their tasks settle | | open |
| W9 | The person clicks while the script waits: `person_acted: true`, and the agent re-reads | | open |
| W10 | A second UI window shows view-only and its input is ignored | | open |
| W11 | A download lands in session scratch and the triggering call reports it | | open |
| W12 | `page.evaluate` in `auto-edits` asks | | open |
| W13 | Element comment on a fixture button | Waits on the comment system | pending |
| W14 | Region comment over the fixture table | Waits on the comment system | pending |
| W15 | arxiv.org cs.DC listing: the host is asked, sources list the browser calls with arxiv.org URLs, the screenshot has a `web:arxiv.org` tool-declared edge | | open |
| W16 | The live view at 390 by 844 | | open |
| W17 | Export a W1 session | | open |
| W18 | `plugin check` rejects a binding importing `requests` | | open |
| W19 | No browser launch under `src/` outside `gact/browser/` | | open |
| W20 | A timed redirect with no call in flight is refused; the panel allows it; with no viewer the next call reports `navigation_blocked` | | open |
| W-L1 | Tauri desktop, "Use my Chrome", `cdp` against a real Chrome | | local phase |
| W-L2 | Sandbox result on stock Ubuntu 24.04, macOS, Windows and an HPC login node | | local phase |
| W-L3 | Input-to-frame latency through the desktop tunnel | | local phase |

## Evaluation

Recorded, not passed: steps, tokens and wall time for W1, W6 and W15 with the browser tools as
`code`, and again with `browser_snapshot` also `direct`.

| Run | `code` only | `browser_snapshot` also `direct` |
| --- | --- | --- |
| W1 | | |
| W6 | | |
| W15 | | |
