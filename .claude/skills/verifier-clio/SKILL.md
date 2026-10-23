---
name: verifier-clio
description: Verify a CLIO change at its real surfaces — the running agent (GACT REST API), the clio-agent CLI, the gact-tui web UI in headless Chromium, and the native Tauri desktop app under Xvfb — on one fresh stack, with captured evidence for each step. Use for /verify, release qualification, or any "does X actually work" question in clio-agent, external/gact-tui or external/clio-agent-marketplace.
---

# Verifier: CLIO

Evidence is what the running system did: a turn's tool calls and text, a CLI
pane, a screenshot you read. Tests, typechecks and CI are not evidence here.

## 0. Scope

Write down, per repo, the range under test and its head:

```bash
git log --oneline <base>..HEAD | wc -l; git diff <base>...HEAD --stat | tail -1
```

clio-agent's PR base is in `gh api repos/iowarp/clio-agent/pulls/<n> -q .base.ref`;
gact-tui and the marketplace are submodules under `external/`. List the user-facing
capabilities the range changes. Each becomes one or more steps below. A
capability you cannot reach is reported BLOCKED with the reason, never skipped
silently.

## 1. Bring up

```bash
bash .claude/skills/verifier-clio/scripts/up.sh          # fresh restart, new run dir
```

It records the heads in `.cloud-run/verify/current/heads.txt`, restarts the
stack detached, and waits for `/v1/sessions` and `/v1/workspaces` to answer.
`/v1/health` answers before those routes do; they return 503 while the task
runtime starts, so do not drive anything before `up.sh` prints `ready`.

Model provider is `claude_code` (the cloud CLI login). Other providers, HPC,
Windows and macOS are not reachable from a cloud session: say so per step.

## 2. Surfaces and drivers

| Surface | Driver | Evidence |
|---|---|---|
| Agent (REST + the model's tool use) | `uv run --no-sync python .claude/skills/verifier-clio/scripts/agent.py ...` | turn captures `turn-*.txt`: every tool call, result, error and text |
| CLI | `bash .claude/skills/verifier-clio/scripts/cli.sh NAME COMMAND...` | `cli-NAME.txt` pane with `[exit N]` |
| Browser UI | `node scripts/cloud/ui_check.mjs --steps FILE --label NAME [--width 390 --height 844]` with helpers from `scripts/ui-steps.mjs` | `.cloud-run/shots/NAME-*.png`, console errors, failed requests |
| Native desktop | `bash .claude/skills/verifier-clio/scripts/native.sh run FILE --label NAME` | `NAME-*.png` and `NAME-report.json` in the run dir |

Agent driver commands:

```bash
A="uv run --no-sync python .claude/skills/verifier-clio/scripts/agent.py"
$A workspace edits                       # scratch workspace inside the run dir -> ws id, folder
$A session "title" --workspace WS --add wtfp --approval auto-edits   # -> sid
$A say SID "message"                     # waits for the turn; prints calls/results/text
$A command SID write-section "input"     # a typed slash command, as the person
$A get /v1/fleet  |  $A patch /v1/fleet/accounts/x '{"delegation_allowed":true}'   # prints HTTP status + body
```

Messages are listed newest first by the API; `agent.py` handles that. A turn
the model ends without the tool you asked for is a finding about the prompt or
the tool surface, not a pass. Re-ask once, plainly; if it still does not,
report what it did.

Browser step files import the helpers:

```js
import * as ui from '../../.claude/skills/verifier-clio/scripts/ui-steps.mjs';
export default async (page, shot) => {
  await ui.openSession(page, SID);
  await ui.expandUsedTools(page);             // tool calls live inside "Used tools"
  await shot('transcript');
  await ui.openPanelTab(page, 'Observability'); // right panel; ?panel= does not open it
  await ui.showCompletedWork(page);           // finished children sit behind this
  await shot('work');
};
```

Put step files in `.cloud-run/verify/current/` (not committed). Read every
screenshot with the file reader; a screenshot you did not read is not evidence.
Run each UI step at 1440x900 and 390x844.

Native desktop: first time in a container run `native.sh deps` then
`native.sh build` (about 15 minutes). The app attaches to the stack's agent
through `CLIO_GACT_URL`. Its step files get an `app` object (`go(route)`,
`click(css)`, `waitFor(css)`, `text(css)`, `js(src)`, `shot(name)`); see the
header of `native-drive.mjs`. Native proves the WebView, the Rust HTTP/SSE
bridge and window chrome on Linux only; Windows and macOS stay BLOCKED here.

## 3. Drive each capability, then push on it

For each capability: the smallest real path that executes it (the person's
action in the UI, the agent's tool call, the CLI command), then at least one
probe off the happy path at the same surface: a wrong model id, an unknown
account, a malformed body, a refusal the model must relay, doing it twice,
after a restart. Probes that hold are findings too.

Destructive or outward actions (publishing, pushing, deleting user data) are
driven only against scratch targets made by `agent.py workspace`.

When a step fails: capture it, then decide whether the change or the
environment caused it. A pre-existing defect you meet is reported, not routed
around.

## 4. Report and clean up

Report in the /verify format: verdict, claim, method, numbered steps
(✅ ❌ ⚠️ 🔍) each with its evidence, findings. Send the screenshots that carry
the verdict with SendUserFile when the session is remote. No partial pass: one
failed or unreached capability makes the verdict FAIL or BLOCKED.

```bash
bash .claude/skills/verifier-clio/scripts/down.sh            # deletes recorded sessions/workspaces, stops the stack
bash .claude/skills/verifier-clio/scripts/down.sh --keep-stack
```

Evidence stays in the run directory; remove it once the report is delivered.

## Gotchas

- `stack.sh start|restart` holds its caller's stdout; run it detached (`up.sh` does).
- After `git stash` or checkout under a running UI, Vite can serve a stale module
  graph ("does not provide an export named ..."); restart the UI.
- A child agent appears in the Observability panel only under Background tasks →
  Show completed work once it has finished.
- The transcript shows a spawn as a tool call, not a child card, after reload.
- Settings pages are addressed by their id in `web/src/lib/application-destinations.ts`
  (`/settings/tools` is MCP tools); an unknown id silently shows General.
- Connect MCP opens on "Remote service"; process settings appear only after
  choosing "Local command".
- `/v1/sessions/{sid}/events` is a live stream; do not curl it expecting JSON.
- Open conversations in their own workspace (`openSession(page, sid, ws)`): the
  wrong workspace in the URL shows "Workspace unavailable".
- Scratch workspaces must be real paths: the permission gate refuses writes
  through a symlinked root ("folder approval cannot override symlink policy").
- Native: the supervisor reads the attach env names of the build's brand; the
  neutral gact-tui build reads `GACT_URL`/`GACT_PORT` (native.sh sets both sets).
  If the first screenshot is "Add an agent service / Local Agent could not
  start", the app did not attach.
- Never `pkill -f` a pattern that appears in your own command line; stop
  processes by recorded PID or process group.
