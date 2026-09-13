# Shared MCP runtime integration (#1319)

CLIO installs one persistent `clio-kit[science]` tool environment for NDP, Geo,
Pandas, and Plot. Each declaration remains `clio-kit mcp-server NAME`, so every
namespace is a separate process while all four processes import dependencies from
the same installation. Normal discovery does not run a solver, build an
environment, hash mutable source trees, or garbage-collect legacy caches.

The current released toolkit pin predates this contract and remains on legacy
runtime semantics:

```sh
uv tool install clio-kit==2.10.6
```

The CLIO installers perform that legacy operation by default until the paired
release exists. They accept `CLIO_KIT_PACKAGE` as an explicit candidate override.
To exercise an unreleased local
candidate without claiming it is published, set it to the absolute wheel path:

```powershell
$env:CLIO_KIT_PACKAGE = 'D:\build\clio_kit-<candidate>-py3-none-any.whl'
.\install\install.ps1
```

```sh
CLIO_KIT_PACKAGE=/mnt/build/clio_kit-\<candidate\>-py3-none-any.whl ./install/install.sh
```

The installer appends `[science]` once. Do not configure separate `uvx` commands
for the four servers because those commands can select distinct package-manager
environments.

For a candidate already installed on `PATH`, run the bounded local qualification:

```sh
uv run --no-sync python -B scripts/qualify_shared_mcp_runtime.py \
  --runtime-root /absolute/path/to/new-qualification-runtime
```

Or install and qualify a local wheel in one operation:

```sh
uv run --no-sync python -B scripts/qualify_shared_mcp_runtime.py \
  --package /absolute/path/to/clio_kit-\<candidate\>-py3-none-any.whl \
  --runtime-root /absolute/path/to/owned-qualification-runtime
```

The report includes the interpreter and prefix, dependency and source identities,
dependency problems, expected namespace tools, real CSV profile/plot calls, and
per-process discovery time. Candidate installation is confined to the explicitly
owned runtime root and never replaces an ambient tool. The script also fails if
normal discovery changes the legacy isolated-runtime cache. `clio-kit runtime-info
ndp geo pandas plot` is the smaller diagnostic when MCP discovery is unnecessary.

Upgrades are package-manager operations performed after stopping the installation's
servers. Keep an older installation for rollback while its processes are live;
CLIO Kit does not mutate or collect shared installations. Its `cache gc` command
only applies to legacy `--isolated` environments and retains live markers.

This local workflow does not claim release publication, HPC filesystem timings,
allocated-byte savings, or live vLLM qualification.
