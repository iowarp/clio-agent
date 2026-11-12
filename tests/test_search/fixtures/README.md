# Recorded search API fixtures

Recorded HTTP exchanges for unit tests of the web/literature search module. Every request
was keyless, sent one at a time at least 2 s apart, at most 5 per API, with
`User-Agent: clio-agent-tests/0.1 (+https://github.com/iowarp/clio-agent)`. Nothing here is
a credential. The `mcp-session-id` values are ephemeral, and the `api_key=YOUR_KEY` strings
are placeholders from the providers' own error text.

File shape: `{"request": {method, url, headers, json|params}, "status", "headers"` (only
content-type, retry-after, rate-limit and mcp-session-id headers)`, "body"}`. JSON bodies are
parsed. SSE, Atom XML and plain-text bodies are stored as raw strings. Trimming rules:
result lists are cut to 5 entries, and any string over 4,000 chars is cut and marked
`…[trimmed]`. In MCP SSE bodies, the JSON inside `result.content[].text` is parsed, trimmed
and re-serialized, so it stays valid JSON. Tool lists and author lists are not trimmed.

## Fixtures (all captured 2026-11-12)

- keenable/search_ok: POST /v1/search/public, 5 results with title/url/snippet/acquired_at; also shows rate-limit headers.
- keenable/search_filtered: `site: arxiv.org` + `published_after: 2025-01-01`; results carry `published_at`.
- keenable/search_missing_title: 400 `missing_app_title` error when X-Keenable-Title is absent.
- keenable/fetch_ok: GET /v1/fetch/public; markdown `content` that the provider truncated (`truncated: true`, `total_chars: 78733`), then cut by us to 4,000.
- exa/tools_list: tools/list without initialize (the server is stateless): schemas of web_search_advanced_exa and web_fetch_exa.
- exa/search_ok: tools/call web_search_advanced_exa (5 results requested). **Partial:** at capture, the inner JSON text was cut at 4,000 chars and later closed by hand, without adding any data. It holds `requestId`, `resolvedSearchType` and one result with only `id`/`url`/`text`. The other 4 results and any fields after `text` were lost. Re-capture after the free cap resets.
- exa/rate_limited: HTTP 429 JSON-RPC error -32000 "free MCP rate limit", `retry-after` and `x-ratelimit-*` headers (reset at 2026-11-13T00:00Z). This was the reply to the search_filtered call, so search_filtered and fetch_ok were not captured.
- firecrawl/rate_limited: HTTP 429 to the first /v2/search call (search_ok). `reason: "credits"`, `retry_after_seconds`. search_research was not attempted.
- you/initialize: MCP initialize result. No `mcp-session-id` is issued (stateless).
- you/tools_list: you-search and you-discover schemas (`count`, `freshness`, `exclude_domains`, `extraction`, …).
- you/search_ok: you-search, 5 `results.web[]` with highlights.
- you/search_filtered: freshness `2025-01-01to2026-11-12` + exclude_domains `["github.com"]`.
- parallel/initialize: MCP initialize result with `mcp-session-id` response header (stateful).
- parallel/tools_list: web_search, web_fetch, send_feedback schemas.
- parallel/rate_limited_search: web_search answered **HTTP 200** with JSON-RPC error -32000 free-tier rate limit. There are no rate-limit headers.
- parallel/rate_limited_fetch: the same error for web_fetch.
- openalex/budget_exhausted: HTTP 429 "Insufficient budget" (the shared per-IP free daily budget), with all `x-ratelimit-*` USD/credit headers. This was the reply to filter_search, so work_by_doi was not attempted.
- crossref/search_ok: /works?query=…&rows=5, polite pool (`x-api-pool: polite-array`).
- crossref/preprint_relation: chemRxiv posted-content with `relation.is-preprint-of`.
- crossref/work_with_references: Nature article, `reference` trimmed to 5 of 128.
- arxiv/search_ok: Atom feed with 5 entries (totalResults 137), complete and untrimmed.
- europepmc/search_ok: resultType=core, 5 of 1,674 hits, cursorMark pagination.
- osti/search_ok: top-level JSON array of 5 records.
- opencitations/references: top-level array, trimmed to 5 of 120.
- opencitations/citations: top-level array, trimmed to 5 of 21.
- datacite/search_ok: JSON:API `data[]` with 5 of 42 DOIs, `meta`, `links.next`.
- jina/reader_ok: r.jina.ai text/plain markdown (`Title:` / `URL Source:` / `Markdown Content:`), cut to 4,000 of 14,668 chars.

## Observed contracts

**keenable**: Results are at `results[]`. `.title`, `.url`, `.snippet` (long extract) and `.description` (empty in every result) are always present. `.acquired_at` (crawl time) is always present, and `.published_at` appears only on some results. The top level also has `query` and `mode` (`"pro"`). Fetch returns `url`, `title`, `content` (markdown), `description`, `truncated` and `total_chars`. Rate headers: `x-ratelimit-limit: 1000`, `-remaining`, `-reset` (ISO time). Errors are `{error, message, type, code, fix, docs_url, retryable, param, request_id}`; a missing title gives 400 `code: missing_app_title`. Filter fidelity: in the 5 kept results, `site` and `published_after` were honoured, since all are arxiv.org and every `published_at` is ≥ 2025-03-27.

**exa** (MCP, stateless, SSE `event: message`): The payload is `result.content[0].text`, a JSON string holding `{requestId, resolvedSearchType, results[]: {id, url, text, …}}`. The title and published-date fields were not observed (see the partial fixture). The text-length cap is `textMaxCharacters`, and the fetch cap is `maxCharacters`. Dates are `startPublishedDate`/`endPublishedDate`. Rate limit: HTTP 429 with `application/json` body `{"jsonrpc","error":{"code":-32000,"message"}}`, `retry-after` in seconds and `x-ratelimit-reset` as epoch ms. Filter fidelity: not observed, because the limit was hit.

**firecrawl**: Rate limit/credits: 429 `{success:false, error, reason:"credits", retry_after_seconds, signup_url}` plus `retry-after`. No success shape was observed.

**you** (MCP, stateless, SSE): `result.structuredContent` mirrors `content[0].text`. Results are at `results.web[]`, with `.url`, `.title`, `.description` (often empty), `.page_age` (ISO, optional), `.contents.highlights[]` (snippets), `.favicon_url` and `.thumbnail_url`. The top level also has `metadata.{search_uuid, query, latency}`. Filter fidelity: both filters were honoured. All 5 `page_age` values fall in 2025-01-16..2026-09-25, there is no github.com result, and undated results were dropped. Note that the tool's own description says `freshness` can only widen a timeframe already named in the query.

**parallel** (MCP, stateful: send the `mcp-session-id` from initialize): The free tier was exhausted, and the limit comes back as **HTTP 200** + JSON-RPC `error.code -32000` with no headers. A client has to inspect the body, not the status. Success shapes and filter fidelity were not observed.

**openalex**: Budget exhausted: 429 `{error:"Rate limit exceeded", message}` with `retry-after` / `x-ratelimit-reset` (in seconds, until midnight UTC), `x-ratelimit-remaining-usd`, `-limit-usd`, `-cost-required-usd`, `-credits-required`, `-onetime-remaining` and `-prepaid-remaining-usd`. No success shape was observed.

**crossref**: Search returns `message.items[]` and `message.total-results`. Single works are at `message`. `title[0]`, `URL`, `DOI`, `issued.date-parts[0]` (which may be `[[null]]`), `published.date-parts` and `abstract` (JATS XML, optional) are the main fields. Other paths are `relation.is-preprint-of[].id`, `reference[].{key, DOI?, unstructured?}` and `reference-count`. Headers: `x-rate-limit-limit` (3 for list, 10 for single), `x-rate-limit-interval`, `x-concurrency-limit` and `x-api-pool`. Query search is relevance-only, and 2 of the 5 items have no abstract.

**arxiv**: Atom `feed/entry`, with `id` (abs URL with version), `title`, `summary`, `published`, `updated`, `author/name`, `link[@title=pdf]` and `arxiv:primary_category`. `opensearch:totalResults` gives the hit count.

**europepmc**: Results are at `resultList.result[]`, with `.title` (contains HTML entities such as `&lt;sub&gt;`), `.doi`, `.firstPublicationDate`, `.pubYear`, `.abstractText` (optional), `.fullTextUrlList.fullTextUrl[].url`, `.source` and `.id`. The top level has `hitCount` and `nextCursorMark`. There is no rate-limit header.

**osti**: A top-level array. Fields are `title`, `doi`, `publication_date` (ISO Z), `description`, `product_type`, and `links[]` where `rel=citation` is the landing page and `rel=fulltext` is the PDF. There are no rate-limit headers. Two of the 5 hits do not mention ADIOS in their metadata, so they were probably matched on full text.

**opencitations**: A top-level array of `{oci, citing, cited, creation, timespan, journal_sc, author_sc}`. `citing` and `cited` are space-separated id strings (`omid:… doi:… openalex:…`), so the DOI has to be parsed out.

**datacite**: Results are at `data[].attributes`, with `titles[0].title`, `url`, `doi`, `publicationYear`, `dates[]{date,dateType}`, `descriptions[].description` and `types.resourceTypeGeneral`. The top level has `meta.total` and `links.next`.

**jina**: text/plain with a `Title:` line, a `URL Source:` line, then `Markdown Content:`. Rate headers: `x-ratelimit-limit: 20, 20;w=60` and `x-ratelimit-remaining`.
