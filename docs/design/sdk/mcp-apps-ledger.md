# Apps ledger: connectors, apps in a tab, ParaView

Rows follow [mcp-apps-surfaces-plan.md](mcp-apps-surfaces-plan.md): decisions `CN` (connectors), `MA` (app host), `PV` (ParaView), scenarios `AQ`. Cross-stream wire shapes are in [mcp-apps-contracts.md](mcp-apps-contracts.md).

- **Branch:** `claude/parallel-agents-plan-uzpzm0` in clio-agent, gact-tui, clio-kit, clio-agent-marketplace and clio-schemas.
- **Bases:**

  | Repository | Base |
  | --- | --- |
  | clio-agent | `develop` at `54ae350af` |
  | gact-tui | `develop` at `d13393f13` |
  | clio-kit | `c22c7e4` |
  | clio-agent-marketplace | `bc5ddb2` (the clio-agent submodule pin) |
  | clio-schemas | `develop` at `fd04806` |

- **References** (read-only clones): ext-apps at `ea3e9d0`, Burette at `b1ac8ae`, ltspice-mcp at `9455f38`.

Status vocabulary (as in the [acceptance ledger](acceptance-ledger.md)):

- `passed (live)`: exercised against a running CLIO, with the sessions, commands and screenshots that were read.
- `tests only`: covered by the named tests and not exercised live.
- `static`: checked by reading the tree.
- `done`: a recorded decision with no behaviour of its own.
- `open`, or `blocked` with the reason.

Live scenarios run on `claude-sonnet-5-5` through the `claude_code` provider. A prompt that names the tool or steps verifies the mechanism only. Only a plain request counts as evidence of the agent's choices.

While the streams work, each keeps its rows in `mcp-apps-ledger.d/<stream>.md`. They are merged here at the end.

## Session environment

| ID | Fact | Evidence | Status |
| --- | --- | --- | --- |
| ENV1 | Docker 29.9.2 runs in the session (overlayfs, cgroup v1). Docker Hub answers anonymous pulls with 429, so images come from `public.ecr.aws/docker/library/*` | `docker run --rm public.ecr.aws/docker/library/debian:bookworm-slim` | done |
| ENV2 | Headless ParaView 6 comes from conda-forge into `/opt/pv6` (micromamba) | see the `pv-cluster` rows | open |

## Decisions

| ID | Decision | Source | Evidence | Status |
| --- | --- | --- | --- | --- |

## Qualification

| ID | Scenario | Evidence | Status |
| --- | --- | --- | --- |
