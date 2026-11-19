# Demo issues on develop: reproduced, fixed, validated (2026-11-19)

The issues below were reported from a Desktop 0.7.1 demo (backend v0.9.7.1). Each one
was checked against develop: clio-agent `8247abe`, gact-tui `22fc042` and the
marketplace commit develop pins, `7d1933c`. Each was reproduced on a live,
isolated develop stack where that was possible. The fixes are on
`claude/develop-ux-fixes` in clio-agent (3 commits) and, because this session cannot
push gact-tui, as patches in [`gact-tui-patches/`](gact-tui-patches/) (5 commits).

Apply the gact-tui patches on a branch from gact-tui `develop`:

```bash
git switch -c claude/develop-ux-fixes origin/develop
git am /path/to/clio-agent/docs/release/develop-ux-fixes/gact-tui-patches/*.patch
```

They were checked with `git am` onto `22fc042`; the resulting tree equals the tested
branch (`407c5a9`).

## Status of each item

| # | Issue | On develop | Change |
|---|---|---|---|
| 1 | Some tool groups did not fold | **Not reproduced.** Three Deep research runs (one stopped, one finished, one in edit mode) folded every group; no call was left without a result. The fold rule waits for every call in a group to settle, so a group stays open only when a call never reports an end. | None. One way it happens was seen (below): lost store writes left a task `active` after its session errored. If it recurs, send the session id and `GET /v1/sessions/<id>/agent-tasks`. |
| 2 | Tiny agent checkboxes | **Present.** `CheckBox` was the bare upstream control. | gact-tui `ClioCheckBox`: CLIO's checkbox; the whole row is the hit target, at least 36 px (44 px on touch). |
| 3 | Chip shows `earthscope.stations.selected recorded` | **Present.** Unknown action names printed raw. | gact-tui: names the control pressed (`“Analyse selected station” recorded`), else "Your choice"; no extra divider. |
| 4 | Deep research tag not at the right | **Present.** It sat about 125 px left of the bubble edge: hidden hover actions after it kept their width. | gact-tui: hover actions come first; the tag ends the row. |
| 5 | Planner parse failure | Handling fixed on develop (marketplace `ca6c89e`, `03ada52`). Three live plans succeeded here. | None. |
| 6 | Raw model reply as the flow error | **Present.** The 2,000-character message was the red headline. | clio-agent: `flow_error` carries `reason` and `message_truncated_chars`. gact-tui: headline is the first line; Details has the error type and full message. |
| 7 | "Task history incomplete" chip unreadable | **Not reproduced.** Readable in both themes (dark: text 236,230,218 on 38,35,31). | None. |
| 8 | Step panel shows the whole run's request | Fixed on develop (gact-tui `f2ef5a7`). | None. |
| 9 | `limit="15"` refused | Present in code; this run's model sent integers, so it did not occur live. | clio-agent: integer, number and boolean arguments sent as JSON text are decoded; other text is still refused. |
| 10 | `run_code` refused in Deep research | **Present.** All five researchers' scripts were refused in one run. | clio-agent: allowed behind an enforced fence, read-only as in Plan mode. Live: 28 of 31 scripts ran, and the run completed with its report saved. |
| 12 | Consent cards: a tiny radio, then Send response | **Present.** A one-choice MCP form ("Allow searches through the literature sources?") rendered a radio list and a separate button. | gact-tui: a form that is one choice of two or three options shows one numbered button per option; pressing it answers. |
| 11 | Stop did not stop the flow | Fixed on develop (#2354). Live: Stop with `children: stop` cancelled the flow and every researcher. | None. |

## Evidence

- clio-agent: ruff, ruff format, mypy (changed modules), `check_file_size`,
  `check_no_class_in_function`, and 265 tests over the touched areas
  (`test_flows/`, tool arguments, grant resolver, code mode, flow summary) passed.
- gact-tui: `pnpm lint`, `tsc -b` and the i18n catalog check passed. The full
  `pnpm test` ran 5,670 tests. One failure was a test that pinned the old raw action
  name, now updated in the lifecycle patch. The other two are in
  `connected-source-picker.test.tsx` ("Use a folder on Agent's computer" not
  found) and fail the same way on clean develop `22fc042`, so they come from
  develop, not this change. New tests: `a2ui-checkbox.test.tsx`,
  `flow-run-parts.test.tsx`, and additions to the lifecycle and prompt-mode tests.
- Live, on an isolated stack built from both branches (Claude via `claude_code`):
  a plain Deep research request ran every researcher script; the tag ends flush with the
  bubble; the gallery checkbox row is 36 px and toggles from its label; a
  flow error, with nodes from the server's own execution view, shows a one-line
  headline and the full message under Details.
- A full Deep research run on the fixed stack (2 GB store) completed: plan, two
  rounds, report saved, every call settled and every group folded.
- Item 3's chip was verified by component tests only. A live check needs an agent
  surface with an action; none was staged.

## Found while validating (not changed here)

- **The cloud stack's 128 MB store fills during a Deep research run once scripts
  run.** `scripts/cloud/stack.sh` sets `CLIO_ARC_CTE_FILE_CAPACITY=128MB`; an explicit
  capacity turns off managed growth. With `run_code` allowed, one run filled
  `storage.bin_node0` (exactly 134,217,728 bytes). Clio-core then refused writes with
  code 12: 53 lost, logged as `clio_core_put_write_lost`. The flow's session went to `error` while its task
  stayed `active`, so the conversation stayed "running" and its waiting call never
  settled. A normal install's store starts at 2 GB or more and grows. A 2 GB rerun
  completed. Two follow-ups: the cloud cap, and a task whose
  session errored should not stay `active`. That is one way item 1's open groups can happen.
- **Two kernels start for one agent at once.** Parallel `run_code` calls from one
  researcher logged `kernel started` twice for the same key. One call then failed in
  6 ms ("the Python kernel did not start" / "did not take its tools" / "stopped"):
  3 of 31 in the fixed run. The develop stack logged the same double start in edit
  mode, so it predates this change; Deep research now runs scripts, so it shows
  up more.
- `stack.sh stop` left the stopped stack's clio-core runtime (`clio_run`) and its
  log pump running once; both exited on SIGTERM.
