# Demo issues on develop: reproduced, fixed, validated (2026-11-19)

The issues below were reported from a Desktop 0.7.1 demo (backend v0.9.7.1). Each one
was checked against develop: clio-agent `8247abe`, gact-tui `22fc042` and the
marketplace commit develop pins, `7d1933c`. Each was reproduced on a live,
isolated develop stack where that was possible. The fixes are on
`claude/develop-ux-fixes` in clio-agent (7 commits) and, because this session cannot
push gact-tui, as patches in [`gact-tui-patches/`](gact-tui-patches/) (7 commits).

Apply the gact-tui patches on a branch from gact-tui `develop`:

```bash
git switch -c claude/develop-ux-fixes origin/develop
git am /path/to/clio-agent/docs/release/develop-ux-fixes/gact-tui-patches/*.patch
```

They were checked with `git am` onto `22fc042`; the resulting tree equals the tested
branch (`8b7fe01`).

## Status of each item

| # | Issue | On develop | Change |
|---|---|---|---|
| 1 | Some tool groups did not fold | **Reproduced** with your EarthScope request through the UI. "Search web" spun for 24 minutes with only a spinner and the elapsed time. It was waiting on a source consent ("Allow searches through Keenable?") that sat behind another card in the question pager, and nothing tied the two together: the consent question carried its call id only in its elicitation block, not in `source.invocation_id`. | clio-agent: an elicitation question names the call waiting on it. Live: the row now reads "waiting for you" and links to the card, its group reads "Waiting for your answer", and settled groups fold. |
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
| 13 | Composer mode as a word on the right | The mode ("Execute", "Deep research") was a word beside the model; the spec of Nov 9 put it there. | gact-tui: the mode is an icon with the other behavior controls on the left, named in its tooltip and accented when not Execute; the spec is updated. Checked at 1280 px and 390 px. |
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
- Live EarthScope run driven through the UI: after a station was picked on the
  agent's map, the chip read "“Analyse selected station” recorded" (item 3). The
  Keenable consent showed two buttons, and pressing the first sent
  `{"action": "answer", "metadata": {"choice": "approve"}}` (item 12).

## Found while validating, and fixed

- **A full store left a run "running" for good, with nothing on screen.** The cloud
  stack capped the store at 128 MB (`scripts/cloud/stack.sh`), which turns off
  growth. With `run_code` allowed, one Deep research run filled it
  (`storage.bin_node0` exactly 134,217,728 bytes). Clio-core then refused writes with
  code 12 (53 lost). The flow's outcome could not be recorded, so its task stayed
  `active` and the conversation stayed "running". Recording nothing is correct:
  CLIO cannot record while the store refuses writes. Saying nothing was the bug.
  - clio-agent: while the task history holds recordings after a refused write, a
    conversation's interactions carry `clio_core_writes_refused` with the same
    sentence a refused send gets ("CLIO's conversation store is full …"). It is
    live and goes once recovery confirms the writes.
  - gact-tui: that notice shows the service's sentence under "Nothing new can be
    recorded"; before, every `clio_core_*` reason read "a damaged data record".
  - Cloud stack: 1 GB instead of 128 MB (`CLIO_ARC_CTE_FILE_CAPACITY` still
    overrides it).
- **Two kernels started for one agent at once.** Parallel `run_code` calls from one
  researcher logged `kernel started` twice for the same key, and one call failed
  within milliseconds ("the Python kernel did not start" / "did not take its tools" /
  "stopped"): 3 of 31 in one run. `kernel_for` started a registered kernel after
  releasing its lock, so a second call took the starting kernel for a dead one and
  replaced it mid-start. A starting kernel is now shared: its start runs once and a
  concurrent caller waits for it (`tests/test_gact/test_kernel_concurrent_start.py`
  fails on the old code).

## Seen once, not changed

- `stack.sh stop` left the stopped stack's clio-core runtime (`clio_run`) and its
  log pump running once; both exited on SIGTERM.
