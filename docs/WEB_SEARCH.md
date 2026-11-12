# Web search

The agent's web and literature tools work on first run with nothing to install:
CLIO sends searches to hosted search services and reads pages itself. There is no
local search server.

| Tool | Plugin | What it does |
| --- | --- | --- |
| `web_search` | `web` | Finds pages through the person's web sources, in their order. |
| `web_fetch` | `web` | Reads one page as Markdown (locally first, a hosted reader if needed). |
| `literature_search` | `literature` | Finds papers, preprints, reports and datasets across scholarly sources. |
| `literature_lookup` | `literature` | One work's metadata, readable URL, references, citers and versions. |

The tools are CLIO built-ins (in-process MCP servers `web` and `literature`, beside
`fs` and `shell`); an agent gets them when its plugins claim them. Code:
`src/clio_agent/search/` and `src/clio_agent/tools/servers/{web,literature}_server.py`.
The design and the evaluation behind it are in
[web-search-evaluation-2026-11.html](design/web-search-evaluation-2026-11.html).

## Web sources

| Source | Free use without a key | Filters it applies itself |
| --- | --- | --- |
| Keenable | 1,000 requests per hour per network | dates, one site, excluded sites |
| Exa | not published | dates, sites, kind (papers, news, PDF) |
| Firecrawl | not published | dates, kind (research, PDF) |
| You.com | 100 per day per network | excluded sites |
| Parallel | not published (a lighter free mode) | none |
| CLIO search service | set by the service | all |

`web_search` tries them in `search.web.order` (the default is the order above). The
first enabled, approved source that is not resting answers. A filter a source cannot
apply is applied by CLIO to its results, and the result says so
(`filters_applied_by_clio`, note `search_filters_approximate`).

**Permissions.** The four tools change nothing on the computer but send queries and URLs
out, so the permission gate treats them as open-world actions: the session's approval
mode decides (ask asks), a playbook step may forbid them, and plan mode allows them like
`web_fetch`.

**Approval.** The first query to a source asks the person (a choice question, also in
bypass mode, never answered by an agent). An approval is saved in `search.approved`;
"Don't use" lasts until the server restarts.

**Rotation.** A source that answers too-many-requests rests and the next one answers,
with a note. When a source's *free allowance* is used up, CLIO asks once for every
waiting caller: switch to the next source, switch and always switch from now on
(`search.web.auto_switch`), add a key in Settings, or stop searching until it resets.

**Keys.** A key is an optional upgrade: Settings → Web search → Add key. Keys live in
CLIO's credential store (`search-provider:<source>`), never in config. A source with a
key moves to the front of the order unless the person keeps the order.

**Results.** The model sees a compact list (title, URL, date, a snippet of
`search.snippet_chars`). The source's full answer is saved under the session's
tool-output folder and named in `payload_file`.

## Reading pages

`web_fetch` downloads the page and turns HTML into Markdown. When the local read is
blocked (401/403/429/5xx), empty or script-only, or the page is a PDF, an approved
web source's hosted reader (Keenable, Exa, Parallel) reads it instead, only for a
public URL: never a private address, an intranet name, a URL with credentials or a
signed/token query. A PDF is also saved locally (`pdf_path`) for `view_pdf`.

## Literature sources

`literature_search` always asks OpenAlex and Crossref and adds sources by the
`field` and `kind` the model chose: arXiv (physical, computing, preprints), Europe PMC
(life sciences, preprints), OSTI.gov (energy, engineering, earth, physical) and
DataCite (datasets). Semantic Scholar answers only with the person's key. Works are
merged across sources by DOI, arXiv id or PMID, and works found by more sources come
first. Sources are paced by their published terms (arXiv: one request every three
seconds). OpenAlex without a key has a small daily budget per network; Settings can
request a free OpenAlex key for the person's email (it works after they click the
emailed link).

## Search service

`search.service.url` points at another CLIO that serves search for a site (one rate
limit and cache for everyone, its own approvals and keys). It answers web searches
first. That CLIO exposes `POST /v1/search`, `POST /v1/fetch` and `GET /v1/limits`
under its normal API authentication; the token goes in the credential store as
`search-provider:service`.

## Settings and API

Settings → Web search shows both lists: order, on/off, approval, key, status in
words, and today's searches and failures from this computer (since CLIO started).
Routes: `GET`/`PATCH /v1/search/providers`, `PUT`/`DELETE
/v1/search/providers/{id}/key`, `POST /v1/search/openalex-key`. Every `search.*` key
is listed in [ENVIRONMENT.md](ENVIRONMENT.md); the SearXNG keys
(`search.backend`, `search.searxng.*`, `search.clio_web_search.url`) are removed and a
leftover one is a typed `config_key_removed` error.

## Typed reasons

Every degradation is a note or error code in the result and the trace
(`clio_agent.search.reasons.SEARCH_REASONS`): `search_not_approved`,
`search_source_rate_limited`, `search_source_allowance_used`, `search_rotated`,
`search_stopped_by_person`, `search_waiting_for_person`,
`search_all_sources_unavailable`, `search_filters_approximate`, `fetch_hosted_reader`,
`fetch_private_url`, `literature_source_budget_used`, and the others in that table.
