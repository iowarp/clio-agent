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
with a note. A source that fails outright (network, 404, 5xx) is asked again after
1 s; if it fails again, `web_search` returns `search_source_temporarily_down` with
advice in words (a temporary problem at the source, not an empty result; call again
and CLIO retries the same source after a 30 s rest, then moves to `next_source`).
Failures during one outage count once, however many researchers hit it. A second
outage in a row moves the search on (`search_source_switched`) and rests the source
for 5 minutes. A source that refuses one request as invalid (400, 413, 414, 422)
moves only that search, with the provider's reason (`search_source_refused_request`). When a source's *free allowance* is used up, CLIO asks once for every
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

`literature_search` always asks OpenAlex and Crossref. With the default `field=any` it
also asks every broad source: arXiv, ChemRxiv, Europe PMC, OSTI.gov and DataCite. A
`field` narrows the search to the sources that cover it: arXiv for physical and
computing, ChemRxiv for chemistry, Europe PMC for life sciences, OSTI.gov for energy,
engineering, earth and physical sciences. `kind=preprint` adds arXiv, ChemRxiv and Europe
PMC; `kind=dataset` adds DataCite. Semantic Scholar answers only with the person's key.

ChemRxiv's own API refuses automated clients, so its preprints are found through
Crossref (ChemRxiv's DOI prefix `10.26434`); those requests share Crossref's queue and
terms. bioRxiv and medRxiv preprints arrive through Europe PMC.

Works are merged across sources by DOI, arXiv id or PMID, and works found by more
sources come first; `found_in` names them. `literature_lookup` follows one work's
references and citing works (OpenAlex, then OpenCitations) and its preprint and
published versions. OpenAlex without a key has a small daily budget per network;
Settings can request a free OpenAlex key for the person's email (it works after they
click the emailed link).

## One request queue per provider

Every session and every agent in it sends provider requests through one queue per
provider for the whole CLIO server (`clio_agent.search.scheduler.SCHEDULER`). To a
provider, CLIO is one well-behaved client, however many researchers run.

- **Tiers.** CLIO reaches each provider on a tier: *no key* (shared by the network),
  *contact email* (Crossref's polite pool), *your key* (free plan) or *paid* (the
  person marks the key as a paid plan, `search.paid`). Each provider's published
  terms per tier (`clio_agent.search.terms.TERMS`) set its rate, how many requests
  run at once and its free allowance: arXiv one request every 3 s; Crossref 5/s one
  at a time, or 10/s three at a time with the contact email; OpenAlex $0.10 a day
  without a key, $1 with a free key (a search $0.001, a list $0.0001, one work free);
  Keenable 1,000 an hour; You.com 100 a day. A paid key has no CLIO allowance.
- **Fair turns.** Sessions take turns; within a session, first come first served.
- **Push-back.** Too-many-requests pauses the provider for everyone (its
  `Retry-After`, else a backoff from 2 s doubling to 60 s), lets one fewer request
  run at once until 20 answers in a row, and puts the request back at the head of
  its line (at most three times). A request that would wait past its patience
  (web 20 s, literature 30 s) leaves with the typed reason: web search moves to
  the next source, literature search notes it. One outright failure (network, 404,
  5xx) is asked again after 1 s before the provider rests.
- **Allowance.** CLIO counts what it used this hour/day (OpenAlex in dollars) and
  stops before the free allowance is used (`allowance_used`, the same question as a
  provider refusal), instead of spending the network's shared allowance to zero.
  Counts start when the server starts; other programs on the network share keyless
  allowances, so the provider's own answer stays the final word.
- **Headers.** Crossref's `X-Rate-Limit-Limit`/`-Interval` and
  `X-Concurrency-Limit`, OpenAlex's `X-RateLimit-Remaining-USD`, and generic
  `X-RateLimit-Remaining`/`-Reset` adjust the queue as answers arrive.

Settings shows each source's tier, the allowance this CLIO used, and how many
requests wait or run.

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
`search_stopped_by_person`, `search_waiting_for_person`, `search_source_retried`,
`search_source_waited`, `search_source_busy`,
`search_all_sources_unavailable`, `search_filters_approximate`, `fetch_hosted_reader`,
`fetch_private_url`, `literature_source_budget_used`, and the others in that table.
