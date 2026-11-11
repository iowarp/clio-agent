# In-app browser with Python control: addendum A

This addendum amends [browser-python-control-plan.md](browser-python-control-plan.md) after a review of the plan against the repositories at `claude/tool-calling-long-horizon` `05bdbb37`. Where the two disagree, this addendum wins. Record each change below as a `BR` decision in `docs/design/sdk/browser-ledger.md`, citing this addendum.

## Base and branch

**Base.** The tool-calling branch has not merged into `develop`, and `develop` has no `gact/code_mode/`. Do not wait for the merge. Build on the head of `claude/tool-calling-long-horizon` in all three repositories, and rebase onto `develop` once that branch merges. This replaces the plan's instruction to stop when `run_code` is not on the base. The only remaining stop condition is that `run_code`, `gact/code_mode/` and the `callers` declarations are missing from the tool-calling branch itself.

**Comments on pages.** The comment system from the LaTeX and comments plan has not started, so no picker code exists yet. Workstream 5 runs last, after that system lands on `develop`. Workstreams 1 to 4 do not depend on it. If it has not landed when workstreams 1 to 4 pass, finish them, record the workstream 5 rows as `pending` on the comment system, and report that.

**Branch.** Use the branch the cloud session is set up to push to, with the same name in clio-agent, gact-tui and clio-agent-marketplace, in place of `claude/browser-python-control`. Write the name in the ledger header.

**Kernel cleanup.** `kernels.close_session` has no caller. If the tool-calling branch fixes this before it merges, drop Phase 1 item 2's kernel half and keep the page half. Otherwise keep the item as written.

## Corrections

**1. Playwright API checks gate two features.** Version 1.63's `show_actions` takes only `cursor`, `duration`, `font_size` and `position`, and 1.63 has no `get_by_ref`. The 1.64 release notes list both a `style` option and `page.get_by_ref`. Pin `playwright==1.64.1` and, in Phase 0, run each call on that version and record the result. The following fallbacks apply if a call is missing or behaves differently:

- **Without `show_actions(style=...)`.** Use the cursor options that exist and leave the annotations unthemed. The agent cursor stays Playwright's. Do not hand-draw one.
- **Without `get_by_ref`.** Remove `ref` from the target vocabulary and from the facade. Request snapshots without refs, and have scripts address elements by role, name, label and text. Do not resolve refs with CLIO code of its own.

**2. Provenance comes from the call record, not a new edge path.** `ingest_edges.py` makes `used web:` edges in two ways. It joins chokepoint egress onto ingest-shaped transforms, or, in step 0 (`declare_web_domains`), it domain-keys an artifact's `used` edge from a URL the producing tool declared. A navigation is not an artifact, so the plan's sentence "every completed navigation records a `used web:` edge" is wrong and is replaced by:

- **Browser calls are their own record.** Each browser call is recorded with its arguments and result like any tool call, and every result carries the page URL. The science log's sources and the Evidence view read browsed pages from those recorded calls, as they read other calls.
- **Artifacts declare the page they came from.** A file a browser tool produces (a screenshot, a download) declares the page URL as a `used` edge through the same declaration seam the web fetch tool uses. Step 0 then keys it as `web:<host>` with `net_mechanism=tool-declared`. Phase 0 names that seam.
- **No new provenance mechanism.** No navigation edges and no per-navigation egress records are added.

W15 changes to match. It passes when the answer's sources list the browser calls with their arxiv.org URLs, and the run's screenshot artifact has a `web:arxiv.org` edge marked tool-declared.

**3. Other Playwright users are exempt.** Rule 3 and W19 exempt `scripts/cloud/`, `scripts/live_verification/`, `site/e2e/` and `tests/`. All of these drive a browser as the person or as a test harness, not as the agent. W19 passes when no code under `src/` outside `gact/browser/` launches or connects to a browser.

## Design gaps, settled

**4. Waiting for the person does not count against the timeout.** A navigation held for a `browser_host` question pauses the action's clock. Time spent waiting for the answer is excluded from `browser.action_timeout_s`, as `run_code` already excludes it from `code.call_timeout_s`. The question goes through the same gate path, so both exclusions come from one mechanism. Stop cancels the question and the action. W2 adds a check that an answer given after 45 seconds still continues the navigation.

**5. Who is asked depends on who caused the navigation.** The route handler attributes each main-frame navigation to the first of these that applies:

1. **An agent action on that page.** A navigation that starts while an agent's browser call on that page is in flight, including that call's `wait_until` window, belongs to that call. The question shows the call and the script it came from, as W2 says.
2. **The person.** A navigation from the person's input, or from the URL bar in the live view, is the person's. It is granted for the conversation, as the plan says.
3. **The page itself.** Everything else is the page's own navigation: a timed redirect, a meta refresh, a script that runs while no agent call is in flight. The page has no one to ask on its behalf, so the navigation is refused unless the host is already granted. The page stays where it was. If the person has the page open in the live view, the panel shows the blocked host as an icon, with the host on hover and a control to allow it for the conversation; allowing it counts as the person's grant and loads the page. The agent's next result on that page carries `navigation_blocked` with the host and the time, so it can ask the person or move on.

W20 is added: a fixture page with a five-second timed redirect to `b.fixture.localhost`, left alone after the agent's script ends. The redirect is refused. The panel shows the blocked host, and allowing it there loads the page. On a second run with no viewer, the agent's next call reports `navigation_blocked`.

**6. The fragment's trigger.** Checked on the tool-calling branch: the fragment gate (`plugins/compose_final.py`, `fragment_reaches`) runs over `self.tools` at prompt build in `gact/agents/builders.py`. Code-only tools stay in that set; they are only marked unlisted by `apply_tool_settings`. A fragment gated on a code-only tool therefore does appear. Change the gate to `when: {tools: [run_code, browser_open]}`. The fragment explains scripts, so it should not appear when `run_code` is absent and the browser tools have fallen back to direct calls. Add to W1: the exact prompt dump (`clio-agent plugin resolve <agent> --exact`) for the standard agent contains `browser.use`, and for an agent without `run_code` it does not.

## Phase 0 additions

Phase 0 gains these items, ahead of the ledger write-up:

1. Run every Playwright call the plan names on 1.64.1 and record each result, including `show_actions(style=...)` and `get_by_ref`.
2. Find the declaration seam the web fetch tool uses for `used` URL edges, and record it.
3. Confirm the route handler can tell a navigation caused by an agent action, one caused by the person's input and the page's own, using the in-flight call and the live view's input timestamps. Record the attribution window.
