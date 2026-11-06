<p align="center">
  <img src="branding/clio/wordmark.svg" alt="CLIO — an AI workspace for scientific data" width="320">
</p>

<p align="center">
  <a href="https://clio.iowarp.ai"><strong>Website & downloads</strong></a> ·
  <a href="https://clio.iowarp.ai/docs/">User guide</a> ·
  <a href="https://github.com/iowarp/clio-agent/releases/latest">Latest release</a>
</p>

CLIO is an **AI workspace for scientific data**, part of [IOWarp](https://iowarp.org).
Work with files, analysis, visualizations and documents in **Desktop or your browser**.
Connect a local model, a provider account, or a model service from your lab.

- Organize work in workspaces and conversations, with persistent ARC memory.
- Extend CLIO with marketplace plugins, agents, tools and flows.
- Inspect HDF5, Parquet and CSV data through MCP tools.
- Run tools where your data lives, locally or on a remote computer.

## Get CLIO

Start at **[clio.iowarp.ai](https://clio.iowarp.ai/#download)** for the Desktop
installer for Windows, Apple Silicon macOS, or Linux. Choose a **bundled** installer:
it includes the CLIO service and its runtimes. Setup prepares the tools and Office
renderer; then open **CLIO Desktop** and choose a model.

You can also install Desktop from a terminal:

**Linux / macOS**

```sh
curl -fsSL https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.sh | bash -s -- --desktop
```

**Windows (PowerShell)**

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/iowarp/clio-agent/main/install/install.ps1))) -Desktop
```

See the **[installation guide](https://clio.iowarp.ai/docs/install/)** for supported
systems, selecting a release, remote deployment and first-launch steps.
An assistant such as Claude Code can follow the **[installation instructions for
AI assistants](https://clio.iowarp.ai/install.md)**.

### Web and servers

For a browser deployment, use the published Web image. From a clone of this repository:

```sh
docker compose up clio-web
# Open http://localhost:8080
```

For a local service with Web, run the install script **without** `--desktop`
(`-Desktop` on Windows), then run `clio` or `clio web`. Use `clio start` for a
headless service, and `clio status`, `clio stop` and `clio logs` to manage it.
The script provisions Python and uv; a release install needs no Git or Go.

**The legacy TUI is frozen and unsupported as of 0.9.5.1.** It is not a valid way
to install, run or verify current CLIO. New releases do not ship its binaries or
Docker image; `clio attach` reports this status instead of running an old binary.
Its [source and integration notes](docs/tui/README.md) remain for a future upgrade.

### Contributors and backend-only installations

`--source` installs `main`; `--dev --source` installs `develop`; add `--ref <ref>`
to select a branch, tag or commit. On Windows use `-Source`, `-Dev` and `-Ref`.
Source mode needs Git and installs the service without building a frontend. See
[contributor setup](docs/CONTRIBUTOR_QUICKSTART.md) for Web/Desktop development.

CLIO 0.9.5.1 uses standard Python 3.14 with the GIL enabled. For a persistent
backend-only installation with the verified SDK roots:

```sh
uv tool install --python 3.14 --with dspy==3.4.0 --with fastmcp==4.1.2 --with fastmcp-tasks==4.1.2 --with mcp==2.2.0 clio-agent==0.9.5.1
clio-agent serve
```

Reserve `uvx` / `uv tool run` for disposable checks. The [installer reference](install/README.md)
covers paths, version selection, lifecycle commands and uninstalling.

## Documentation and contributing

Use the **[website documentation](https://clio.iowarp.ai/docs/)** for installation,
models, workspaces, plugins and examples. Repository references:

- [Architecture](docs/CLIO_AGENT_ARCHITECTURE.md) and [repository documentation index](docs/README.md)
- [Contributor quick start](docs/CONTRIBUTOR_QUICKSTART.md) and [AGENTS.md](AGENTS.md)
- [Adding tools](docs/MCP_TOOL_INTEGRATION.md) and [plugins, agents and flows](docs/PLUGINS_AND_FLOWS.md)
- [Roadmap](docs/design/roadmap.md)

## License and citation

BSD-3-Clause.

```bibtex
@software{clioagent2026,
  title  = {CLIO Agent: Autonomous Agent for Scientific Data Management},
  author = {IOWarp Team},
  year   = {2026},
  url    = {https://github.com/iowarp/clio-agent},
}
```
