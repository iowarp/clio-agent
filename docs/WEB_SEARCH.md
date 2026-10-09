# Web search

The agent's `web_search` tool works on first run without containers: CLIO installs
its own private [SearXNG](https://github.com/searxng/searxng) on this computer and
routes the tool to it. A CLIO Web Search gateway, on this or another machine, is
the alternative.

## Choosing the backend

| `search.backend` | What answers `web_search` |
| --- | --- |
| `local_searxng` (default) | CLIO's private SearXNG: the managed `searxng` service (variant `native`) on this computer's loopback. |
| `clio_web_search` | The CLIO Web Search gateway at `search.clio_web_search.url` -- a CLIO-managed deployment, or one on another machine so search traffic leaves from that machine's address instead of yours. |
| `none` | Search is off. `web_search` answers with the typed error `search_not_configured` and the fix. |

```yaml
# config.yaml
search:
  backend: clio_web_search
  clio_web_search:
    url: http://search-host:8089
```

All `search.*` keys, their environment overrides and defaults are in
[ENVIRONMENT.md](ENVIRONMENT.md). One abstraction, `clio_agent.search.backend`,
resolves the backend: it points the Web MCP server (`clio-kit mcp-server web`)
at it when that server starts, and answers a `web_search` call with
a typed error and the fix when the backend cannot serve:
`search_not_configured` (search is off or misconfigured),
`search_backend_starting` (CLIO is installing or starting its SearXNG, or the
gateway answers `/readyz` with "not ready yet": retry shortly),
`search_backend_stopped` (CLIO's SearXNG is installed but stopped: start it in
Infrastructure > SearXNG) or `search_backend_unavailable` (failed or
unreachable). A Web MCP declaration that names its own endpoint
(`--remote-url`, `--address`, `--provider` or a `WEB_*` variable) is left as
declared. `GET /v1/search/backend` reports the resolved backend and whether it is
ready; `POST /v1/search/query {"query": ...}` runs one search through it.

You do not declare the Web MCP server yourself. Unless `search.backend` is `none`,
CLIO declares it for the main agent as namespace `web` (`clio-kit mcp-server web`,
source `clio-default`), loaded by default. Your own Web MCP declaration (any name)
replaces it and is also loaded by default. Every Web MCP spawn gets
`WEB_STATE_DIR` under CLIO's data directory (`<data>/web-mcp`), because the
server's own default (`~/.config/clio-kit`) is not writable under the tool
sandbox. If `clio-kit` is not installed, nothing is declared, and the server log
records `web mcp not declared reason=launcher_missing`.

## The private SearXNG

* **First run.** With `search.backend: local_searxng` and
  `search.local_searxng.auto_install: true` (both defaults), the server's boot
  installs SearXNG when it was never installed and starts it when it is not
  running -- unless your last action on it was stop, uninstall or delete. Each
  step is an ordinary operation in Infrastructure > SearXNG, where you can also
  install, start, stop, check status and logs, verify, uninstall and delete its
  data yourself.
* **What gets installed.** SearXNG at the commit CLIO Web Search pins
  (`e8e710e42a3ab2bce27d1f97e51d8d4ccaa80871`), downloaded as that commit's
  source archive (an archive that is not that commit is refused), in its own uv
  environment under the service directory -- never CLIO's environment -- with the
  exact `requirements.txt` / `requirements-server.txt` pins of that commit. It is
  served by Granian, SearXNG's own pinned WSGI server, which has wheels for Linux,
  macOS and Windows (uWSGI does not run on Windows).
* **Settings CLIO generates.** Bound to `127.0.0.1` only; the bot limiter is off
  (this is a private instance, not a public one); the JSON format is on; no
  Valkey. A secret key is generated per installation into a `0600` file on the
  host and reaches SearXNG through its environment, so the settings file holds no
  secret. An HTTPS/HTTP proxy in the environment is applied to SearXNG's outgoing
  requests in memory.
* **Proof it is ours.** Readiness requires `/config` to report an instance name
  derived from the installation's secret, and `/search?format=json` to answer.
  `verify` runs one real query and records `result_count`.

### Engines

Enabled by default: `duckduckgo`, `brave`, `mojeek`, `qwant`, `startpage`,
`wikipedia` (general) and `arxiv`, `crossref`, `semantic scholar`, `pubmed`
(scholarly). Safe search is moderate, the language is `en`, and each engine gets
10 seconds (`search.searxng.*`, or the service's form in Infrastructure).

Engines operated under Chinese jurisdiction (`baidu`, `sogou`, `360search`,
`chinaso`, `quark`, ...) are never enabled by listing them in
`search.searxng.engines`; each must also be named in
`search.searxng.opt_in_engines`.

Engines that need an API key (`braveapi`, `core.ac.uk`, `springer nature`,
`marginalia`) read it from CLIO's credential store, saved with
`PUT /v1/search/engine-keys/{engine}` (`{"api_key": "..."}`). The key reaches
SearXNG only through the process environment at start -- never a settings file or
a command-line argument.

### Platforms

Linux is supported and live-tested. macOS uses the same POSIX supervisor and is
offered, but is not live-tested. Windows is not offered yet: CLIO's native
service supervisor is POSIX-only (use `clio_web_search` there). The launcher is
ready for it: SearXNG's `searx/valkeydb.py` does a bare `import pwd`, and on
Windows CLIO's launcher installs a CLIO-owned `pwd` stand-in (`clio_pwd_shim.py`)
before SearXNG is imported, instead of modifying SearXNG.

## Licensing

SearXNG is licensed AGPL-3.0-or-later. CLIO does not ship it: it is downloaded
unmodified from its upstream repository at install time and runs as a separate
process that CLIO talks to over HTTP on the loopback. CLIO's own code (BSD-3-Clause)
is not combined with SearXNG; the CLIO launcher and `pwd` stand-in that run in
SearXNG's interpreter are separate CLIO files. Running a modified SearXNG as a
network service for others carries AGPL obligations; CLIO's private,
loopback-only instance is unmodified.
