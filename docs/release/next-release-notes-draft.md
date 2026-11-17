<!--
DRAFT for the next CLIO release page. Not published. The version is a placeholder
(X.Y.Z) because choosing it is a release-morning decision. Before publishing:
replace X.Y.Z, re-check every #number with `gh pr view N`, and drop any section
whose change is reverted or not shipped. Written to the release-clio style rules
(no em or en dashes, no bold-name bullets, user-facing wording).
-->

# vX.Y.Z

This release lets CLIO connect to outside tools from a conversation, keep app views open beside it, and run agents from your own Python scripts. Stopping and restarting work is also more predictable, and there is a new way to report a problem.

### Connectors you add from a conversation

CLIO now has a catalog of connectors for outside tools, and every clio-kit server is available as one. When an agent needs a connector that is off, it asks you in the conversation and shows a setup card for anything the connector needs; secrets go to the credential store, not the chat. Values you set in one conversation stay in that conversation. (#2358)

### Tool servers install when you first need them

The installer no longer builds every tool server up front, which used to take minutes on a new machine. A server is prepared the first time an agent that uses it loads. If you install for offline use, pass `--prepare-tool-servers` to `install.sh` or `-PrepareToolServers` to `install.ps1` to keep the old behavior. (#2358)

### App views in a tab, fullscreen or a dashboard

Views from MCP apps can now open in their own tab, go fullscreen or sit in a dashboard, and they come back after a restart. The agent can look at a view you are both seeing, and you can comment on a region of it. 3D model files get the same treatment. (#2358)

### Choose which plugins the agent may offer

You can now pick the plugins the agent is allowed to suggest. When it needs one, it shows a short card you can accept or ignore, and it can search beyond your list by words or category. (#2364)

### Run agents from Python

`pip install clio-agent` now includes a `clio` Python package. A script can run an agent or a flow and read its result; it uses a CLIO server already running on this machine or starts one, and a server the script started stops when the script is done. Script runs never stop to ask questions, and you can cap them by turns or by cost. See `src/clio_agent/client/README.md`.

### Stop and restart

Stop now reaches the work a conversation started, such as child agents and research flows, and asks whether to stop them too or keep them running. Restarting a stopped run keeps its finished work and re-runs only what had not finished, and work interrupted by a crash or a shutdown resumes instead of being lost. (#2354, #2365)

### Report a problem

CLIO can now produce a problem report with recent logs (secrets removed), the service's health and, if you choose, the conversation you were in, ready to download and attach to an issue. (#2365)

### Read S3 data in place

You can link an S3 bucket read only by prefix and read files from it by byte range, so large HDF5 and NetCDF files are not downloaded whole. (#2358)

### Commit to git from a conversation

The agent can put a folder under git and commit to it with a dedicated tool that never runs hooks from the repository, and it can no longer write inside a repository's `.git` folder. (#2305)

### KiCad and outside-the-sandbox commands

A KiCad connector covers board design from parts to a routed board and its manufacturing files. When a command genuinely has to run outside the sandbox, CLIO asks you every time, for that exact command only, and never takes a password. (#2364)

## Fixed

- A conversation left with an unanswered tool call no longer blocks every later message; the missing result is recorded as unknown. (#2365)
- When the disk holding CLIO's store is nearly full, writes are refused with a clear message instead of damaging the store, and they resume once space is freed. (#2365)
- A saved model server lists its models again. (#2365)
- Exporting a conversation no longer fails on a size limit for the offline renderer.

## Changed

- A fresh install puts the default workspace in a `workspace` folder inside CLIO's data folder instead of the folder CLIO was started from. Existing workspaces keep their folder. (#2358)
- Answering a permission question with a choice it did not offer now returns `invalid_action` and lists the choices, instead of an internal error. (#2364)
- clio-kit is pinned at the version this release ships with (the pin is set during the release).

## Breaking

- In the Python client, `ClioClient(base_url, headers=...)` is now `ClioClient(options=ClioOptions(...))`, and `routing_mode` is gone. Creating a client connects to a server or starts one.
- A server that the interactive `clio-agent` command starts now stops when its last client leaves.
- A turn cut off by a service shutdown is marked interrupted and resumes after the restart, instead of being marked stopped. (#2365)

See the [CHANGELOG](https://github.com/iowarp/clio-agent/blob/main/CHANGELOG.md) for the full list of API changes.

```sh
uv tool install --python 3.14 --with dspy==3.4.0 --with fastmcp==4.1.2 --with fastmcp-tasks==4.1.2 --with mcp==2.2.0 clio-agent==X.Y.Z
```
