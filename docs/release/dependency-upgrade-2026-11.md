# Dependency upgrade, 2026-11

Branch `chore/deps-upgrade-2026-11`, from `develop` at `aa7304231`. Scope: every
package in `pyproject.toml` and `uv.lock`, extras included, except `iowarp-core`
(moved to 2.5.0 in another session) and `clio-kit` / `clio-schemas` (release
pins owned by the release process). Commits are grouped by risk: patch/minor
first, then each major, then the web, MCP and provider stacks, each with the
fixes it needed.

## Outcome

- **Upgraded:** 132 locked packages (table below). Among them: fastmcp and
  fastmcp-tasks 4.2.4, mcp 2.3.0, pydocket 0.31.0, pydantic 2.14.4, FastAPI
  0.165.0, sse-starlette 3.7.0, litellm 1.110.2, claude-agent-sdk 0.2.187,
  github-copilot-sdk 1.0.35, openai-codex-cli-bin 0.171.0, Playwright 1.65.2,
  truststore 0.10.5, rich 15, websockets 17.4, mypy 2.4, ruff 0.17,
  huggingface-hub 2.4.
- **Held back:** DSPy stays at 3.4.0 and openai at the latest 2.x (2.54.0).
  Both are explained under [Decisions for the user](#decisions-for-the-user).
- **Unchanged by design:** iowarp-core 2.4.1, clio-kit, clio-schemas.
- **Removed from the tree** because nothing needs them any more: keyring,
  jeepney, secretstorage, jaraco-*, pywin32-ctypes and more-itertools (fastmcp 4.2
  dropped keyring), pyee (Playwright 1.65), docutils, babel, tld, cligj,
  click-plugins, antlr4-python3-runtime.

Lower bounds were raised only where the code now needs the new version: the
exact pins that were already exact (fastmcp, fastmcp-tasks, mcp, pydocket,
litellm, playwright, truststore) and the provider SDK floors, which
`scripts/bump_provider_sdks.py` moves by policy. The pydantic cap moves from
`<2.14` to `<2.15`: it only existed to keep the resolver off the 2.14.0 alphas.

## What the upgrade needed

Each of these is a root fix in its own commit:

1. **h5py 3.16 refuses a second import in one process.**
   `test_list_agents_does_not_import_scientific_tool_servers` popped `h5py` from
   `sys.modules` and never restored it, so every later HDF5 test on that xdist
   worker failed with `Can't register conversion function`. It now uses
   `monkeypatch.delitem`.
2. **The bundle-lock fork test read the real lock's beartype fork,** which the
   re-lock unified. It now tests the fork-selection rule on a synthetic lock.
3. **The document stack lock must match the agent lock** where its bounds allow
   (`test_document_lock_matches_the_agent_lock_where_its_bounds_allow`). It was
   re-locked with each group.
4. **ruff 0.17 also formats Python code blocks inside Markdown.** Here Markdown
   is prompt, skill and doc prose an agent reads, so `[tool.ruff.format]`
   excludes `*.md`. 18 Python files take the 0.17 style; one lambda whose `noqa`
   the new layout displaced became a `def`.
5. **mypy 2.4** wants annotations it used to infer (two `code_only` frozensets
   and the `run_code` chunk stream). tokenizers 0.23 now ships typed stubs, and
   `tuple(o)` widened its `(start, end)` offsets. `mypy src/` is left with the
   three `organization_branches.py` errors `develop` already has.
6. **fastmcp 4.2's default transport session is `VersionAwareClientSession`**
   (it skips output-schema checks for calls pinned to an unlisted tool version).
   The native-task observation transport only accepted plain `ClientSession`,
   so every native-task client refused to connect. It now composes over the
   SDK's own default, read from `TransportOptions` rather than named, and still
   refuses any unreviewed session class. `_parse_call_tool_result` forwards
   fastmcp 4.2's new `pinned_version`.
7. **openai 3 imports aiohttp unconditionally.** `gact.app` preloads openai on
   purpose (the DSPy lazy-loader race, #1551), so taking openai 3 would load
   aiohttp on every HTTP start, which
   `test_http_startup_leaves_unused_providers_and_servers_unloaded` forbids.
   openai is held at 2.54.0, the latest 2.x that litellm 1.110.2 accepts.
8. **Pins mirrored outside `pyproject.toml`** follow the tree: `release.yml`'s
   wheel and registry smokes, the model router's `LITELLM_VERSION` and the
   site's router table, the truststore root in `clio-bundles.yml`, the native
   task contract test and the browser probe note. The install-policy test now
   separates this tree's roots (`EXPECTED_*`) from the published release's roots
   (`DOCUMENTED_*`). The README/INSTALL commands and the launcher's `update`
   install the latest PyPI release (0.9.7), and a newer tree's roots cannot
   resolve against it. **The release that publishes this tree must move those
   commands, `install/clio`'s update roots and the `DOCUMENTED_*` constants to
   the new pins.**

Checked and found safe without a change:

- **huggingface-hub 0.35 → 2.4:** `HfApi.model_info(files_metadata=True)`,
  `RepoSibling` size/lfs/blob_id and the `snapshot_download` arguments CLIO
  passes keep their shapes. `HfHubHTTPError` now derives from `httpx2`, not
  requests, and CLIO catches neither.
- **websockets 15 → 17:** CLIO uses only the asyncio client (`connect` with
  `additional_headers`/`open_timeout`/`ping_*`, `max_size=None`, positional
  `send`). The 17.0 keyword-only and threading `sock` changes don't touch it.
- **pydantic 2.14.2** rejects JSON keys that match only an unused alias. CLIO's
  one strict aliased model also sets `populate_by_name`, so both names stay in use.
- **mcp 2.3's new client-level HeaderMismatch retry** lives in `mcp.Client`.
  fastmcp calls `ClientSession.call_tool`, so CLIO's own one-shot retry
  (`tools/mcp_header_mismatch.py`) is still the only one.
- **fastmcp 4.2.4 private members** used by `tools/task_client.py`
  (`_session_kwargs`, `_init_timeout`, `_cache_arg`, `_extensions_arg`,
  `_result_claims_arg`, `_transport_options`, `_resolve_claimed_result`,
  `ToolTask._client`, `FastMCP._lifespan_manager`) and the constructor
  arguments it forwards are unchanged.
- **FastAPI 0.164/0.165** body and form limits default to unlimited.
  **sse-starlette 3.7** adds a 30 s `cleanup_timeout`.

## Adopted from the new versions

Each in its own commit with tests:

- **fastmcp-tasks routes task requests by `taskId` itself** (`name_param` on
  `GetTaskRequest`, `UpdateTaskRequest`, `CancelTaskRequest`). CLIO's three
  `Named*Request` subclasses and the per-request re-validation that copied every
  `tasks/get`, `tasks/update` and `tasks/cancel` are deleted.
- **claude-agent-sdk accepts mcp 2** (`mcp<3,>=1.23` since 0.2.187, the new
  floor). The `[tool.uv] override-dependencies` workaround from #1107 is
  deleted. The lock resolves the same packages and no installer passes its own
  override.
- **Security fixes taken just by upgrading:** fastmcp 4.2.1/4.2.4 (JWT `nbf`,
  `required_scopes` with a custom verifier, OAuth client issuer binding, the
  double-redeem race GHSA-78c8-3jhx-5mg7); pydantic 2.14.2 (GHSA-3r8g-cqwr-jr29)
  and constant-time `SecretStr` comparison; websockets 16.1 (credentials
  stripped on cross-origin redirects) and 17.4 (fragment-count limit); mcp 2.3
  (bounded SSE events, below); litellm 1.110.2 (`/key/info` isolation, which
  matters only for the model router's proxy).

## Decisions for the user

Not implemented on this branch: each one changes behaviour, a default, the agent
loop, a provider or durability.

### 1. DSPy 3.4.0 → 3.5.0 (held)

- **Value:** the bundled lm15 2.2 engine with no LiteLLM fallback (LiteLLM and
  the OpenAI SDK become optional for DSPy), canonical lm15 requests and responses
  through every adapter, tools that return images and files as content blocks,
  ReActV2 with structured history, and lm15 streaming and reasoning coverage.
- **Risk:** 3.5 removes the LM entry points CLIO's LM layer is built on.
  - `LM.__call__`/`acall` no longer take `messages=`, and
    `LM.forward`/`aforward` are gone. Used by `lm/hooked_lm.py`,
    `lm/admitted_dspy.py`, `lm/attention_dspy.py` and `lm/attention_lm.py`.
  - `Adapter.format` returns a `Prompt` (lm15 `Message`s plus `system`)
    instead of message dicts, and `_call_postprocess` takes lm15 `Response`s.
    Used by `lm/adapters.py` (the context pre-flight and the strict guided
    JSON adapter).
  - `Tool.format_as_litellm_function_call` is removed in favour of
    `Tool.as_function_tool()`. Used by `gact/agents/clio_react.py`,
    `lm/engines/text_tools.py` and the override in `tools/dspy_mcp_tool.py`.
  - The default adapter becomes `XMLAdapter`, and `dspy.ReAct` is now ReActV2.
    CLIO sets its adapters explicitly and runs its own loop, so these two
    matter less.
  - Every lm15 symbol CLIO imports still exists in 3.5, so the break is in
    those call shapes, not the imports.
- **Measured:** this branch with only `dspy==3.5.0` changed. Across the 30 LM,
  provider and agent-loop test files (`tests/test_lm`, `tests/test_providers`,
  `test_clio_react*`, `test_turn*`), 118 of 2,172 tests fail, and only one of
  those failures is in `develop`'s baseline:
  - 75 hit `'Tool' object has no attribute 'format_as_litellm_function_call'`.
  - 21 hit `lm(messages=[...]) was removed in DSPy 3.5`, which points to the
    lm15 `Request` migration.
  - 4 hit the removed `LM._wrap_litellm_exception`.
  - The rest are assertions downstream of those.
- **Recommendation:** port it as its own change inside the agent-loop campaign
  (`docs/design/sdk/megachange-plan.md`), not as a version bump. Read the
  upstream guide (`dspy.ai/current/community/upgrading-to-3-5/`) first, move the
  LM wrappers to the `lm(Request)` path the loop already uses, build tool specs
  with `Tool.as_function_tool()`, then grind live scenarios on every provider.
  Outputs can change even where the code compiles.

### 2. openai 2.54 → 3.x (held)

- **Value:** the current OpenAI SDK line. CLIO doesn't call the SDK directly;
  litellm does.
- **Risk:** openai 3 imports aiohttp at import time, and `gact.app` preloads
  openai to avoid the DSPy lazy-loader race (#1551). Every server start would
  load aiohttp, which the startup-import test forbids.
- **Recommendation:** hold. Revisit when the preload can go (DSPy 3.5 no
  longer needs openai at all), or if you decide aiohttp at startup is acceptable.

### 3. 1 MiB cap on remote MCP SSE events (taken with the upgrade, default kept)

- **Value:** mcp 2.3 and fastmcp 4.2 bound one Streamable HTTP SSE event at
  1 MiB (it was unbounded), so a misbehaving remote server can't exhaust memory.
- **Risk:** a user-configured remote HTTP MCP server whose single tool result
  exceeds 1 MiB (large base64 images, for example) now fails that call with a
  transport error. Stdio servers, including every built-in one, are unaffected.
- **Recommendation:** keep the default. If users hit it, expose
  `max_sse_event_size` as a per-server setting in the MCP server configuration
  rather than disabling the cap.

### 4. Request body limits on the GACT server (FastAPI 0.164/0.165)

- **Value:** `FastAPI(max_body_size=...)` and per-route limits bound request
  bodies and form parsing, a cheap guard when the server is reachable beyond
  localhost.
- **Risk:** artifact and file upload routes need larger, per-route limits. A
  global default set too low breaks uploads.
- **Recommendation:** worth doing as a separate change: an app-wide default
  plus explicit higher limits on the upload routes, with tests for each.

### 5. Smaller opt-ins, each needs a design call

- **fastmcp 4.2.3 `parent_task_spans`:** links native task spans to the span
  that started them. Useful for tracing native tasks. Try it once OpenTelemetry
  export is on.
- **Playwright 1.65 `locator.normalize()`:** stable locator strings to pair
  with `get_by_ref` in the in-app browser. It changes what the agent sees, so it
  belongs in the browser campaign.
- **websockets 17 `reconnect_delays`:** not recommended for the Codex socket,
  whose resend-in-full and kept-socket semantics are CLIO's own.

### 6. Follow-ups found on the way (pre-existing, not caused by this branch)

- `mypy src/` already fails on `develop` with three `attr-defined` errors in
  `gact/storage/organization_branches.py` (attributes set on the loop object).
- litellm's next line (1.112 dev) carries breaking `fix(responses)!` and
  `fix(anthropic)!` changes. Read them before the next bump.

## Evidence

### Tests

Full suite (`run_tests.py tests/ -m "not integration" -n logical`, Python 3.14,
4 workers), compared failure by failure with an unchanged `develop` run in the
same container:

| Run | Passed | Failed | Errors | Skipped | New vs develop |
| --- | ---: | ---: | ---: | ---: | --- |
| `develop` baseline | 17,234 | 38 | 36 | 133 | — |
| Patch/minor group | 17,213 | 59 | 36 | 133 | 21, all fixed in `71a8ddd56` |
| Majors (rich, websockets, mypy, transitive) | 17,234 | 38 | 36 | 133 | none |
| Final branch, with CI's `-W error::DeprecationWarning` | 17,237 | 36 | 36 | 133 | none |

- The 74 `develop` failures and errors come from this container, not the
  code. Most are marketplace-plugin fixtures: the `external/clio-agent-marketplace`
  submodule could not be fetched here. The rest are kernel/`run_code`
  probes, the decomposition route-count guard and launcher portability.
- The two baseline failures that pass on the final branch are Copilot runtime
  discovery tests. They need the copilot extra, which the final environment
  installs as CI does.
- The web, MCP and provider groups were checked with targeted runs:
  - Web: 3,110 tests.
  - MCP: 2,700 tests in 121 files. The first run found the fastmcp 4.2
    native-task break fixed in `5ff1bbbf2`.
  - Provider: 2,400 tests.
  - Then the full final run above, with CI's deprecation-as-error flag.
- One full run on the provider group showed 7 memory-service tests losing
  clio-core search under load. They pass alone and pass in the final full run.
- Lint: `ruff check`, `ruff format --check` (src, tests, scripts),
  `check_file_size.py`, `check_no_class_in_function.py` and
  `check_bundle_matches_lock.py --resolve` all pass. `mypy src/` reports only the
  three errors `develop` already has.

### Live stack (claude_code provider, `claude-sonnet-5-5`)

`bash scripts/cloud/stack.sh start` with isolated state, driven over REST and in
the web UI:

1. A plain turn answered `ready`.
2. "What is the sum of the value column in numbers.csv in this workspace?"
   The agent called `fs_glob` and `fs_read_file` through the MCP gateway and
   answered 16. This is a plain request, so it shows the agent's own tool choice.
3. "Which version of Python is installed on this machine?" The agent chose
   `shell_bash`. The permission gate held it for approval, and once approved it
   ran as a native task on the fastmcp 4.2.4 shell server and answered 3.14.6.
4. Session reload: the agent server was stopped and started again (new PID).
   The session kept its 6 messages, and "What was the sum you computed earlier,
   and from which file?" answered 16 from `numbers.csv`.
5. The web UI build sent a turn and streamed the answer, with no console errors
   and no failed requests.

`uv run src/clio_agent/ui/cli.py doctor` exits 0. With the stack's provider
settings every check is ready or skipped. `lm_provider` reads "degraded"
because Claude Code's sign-in has not been verified from the Models page,
which is normal for a fresh CLI-only check.

## Version table

Current is `develop`'s lock, latest is the newest release on PyPI at the time
of writing, and locked is this branch. Changed and pinned packages only;
clio-kit and clio-schemas are unchanged by design.

| Package | Kind | Current | Latest | Locked | Bump | Outcome |
| --- | --- | --- | --- | --- | --- | --- |
| adios2 | direct | 2.12.1.1001 | 2.13.0.1001 | 2.13.0.1001 | minor/patch | upgraded |
| aiofile | transitive | 3.9.0 | 3.12.3 | 3.12.3 | minor/patch | upgraded |
| aiohappyeyeballs | transitive | 2.6.1 | 2.7.1 | 2.7.1 | minor/patch | upgraded |
| annotated-doc | transitive | 0.0.5 | 0.0.7 | 0.0.7 | minor/patch | upgraded |
| annotated-types | transitive | 0.7.0 | 0.8.0 | 0.8.0 | 0.x minor | upgraded |
| antlr4-python3-runtime | transitive | 4.9.3 | removed | removed |  | dropped from the tree |
| anyio | transitive | 4.15.1 | 4.16.0 | 4.16.0 | minor/patch | upgraded |
| ast-serialize | transitive | — | 0.13.1 | 0.13.1 |  | new transitive |
| attrs | transitive | 25.4.0 | 26.1.0 | 26.1.0 | calver | upgraded |
| authlib | transitive | 1.9.1 | 1.9.10 | 1.9.10 | minor/patch | upgraded |
| babel | transitive | 2.18.0 | removed | removed |  | dropped from the tree |
| beartype | transitive | 0.22.5,0.23.1 | 0.23.1 | 0.23.1 | 0.x minor | upgraded |
| boto3 | transitive | 1.43.101 | 1.44.9 | 1.44.9 | minor/patch | upgraded |
| botocore | transitive | 1.43.101 | 1.44.9 | 1.44.9 | minor/patch | upgraded |
| cachetools | transitive | 6.2.1 | 8.0.2 | 8.0.2 | **major** | upgraded |
| caio | transitive | 0.9.25 | 0.12.10 | 0.12.10 | 0.x minor | upgraded |
| certifi | transitive | 2026.10.23 | 2026.11.20 | 2026.11.20 | calver | upgraded |
| cffi | transitive | 2.0.0 | 2.1.1 | 2.1.1 | minor/patch | upgraded |
| charset-normalizer | transitive | 3.4.4 | 3.5.2 | 3.5.2 | minor/patch | upgraded |
| claude-agent-sdk | direct | 0.2.159 | 0.2.187 | 0.2.187 | minor/patch | upgraded |
| click | transitive | 8.3.3 | 8.5.1 | 8.5.1 | minor/patch | upgraded |
| click-plugins | transitive | 1.1.1.2 | removed | removed |  | dropped from the tree |
| cligj | transitive | 0.7.2 | removed | removed |  | dropped from the tree |
| cloudpickle | transitive | 3.1.2 | 3.1.3 | 3.1.3 | minor/patch | upgraded |
| contourpy | transitive | 1.3.3 | 1.4.0 | 1.4.0 | minor/patch | upgraded |
| courlan | transitive | 1.4.0 | 1.5.0 | 1.5.0 | minor/patch | upgraded |
| coverage | transitive | 7.11.0 | 7.17.4 | 7.17.4 | minor/patch | upgraded |
| cronsim | transitive | 2.7 | 2.8 | 2.8 | minor/patch | upgraded |
| cyclopts | transitive | 4.5.1 | 5.5.2 | 5.5.2 | **major** | upgraded |
| dateparser | transitive | 1.4.3 | 1.5.1 | 1.5.1 | minor/patch | upgraded |
| dnspython | transitive | 2.9.0 | 2.9.2 | 2.9.2 | minor/patch | upgraded |
| docstring-parser | transitive | 0.17.0 | 0.18.0 | 0.18.0 | 0.x minor | upgraded |
| docutils | transitive | 0.22.2 | removed | removed |  | dropped from the tree |
| dspy | direct | 3.4.0 | 3.5.0 | 3.4.0 | minor/patch | held: 3.5 removes the LM entry points CLIO's LM layer calls (see Decisions) |
| email-validator | transitive | 2.3.0 | 2.4.0 | 2.4.0 | minor/patch | upgraded |
| exceptiongroup | transitive | 1.3.0 | 1.3.2 | 1.3.2 | minor/patch | upgraded |
| fastapi | direct | 0.162.0 | 0.165.0 | 0.165.0 | 0.x minor | upgraded |
| fastmcp | direct | 4.1.2 | 4.2.4 | 4.2.4 | minor/patch | upgraded |
| fastmcp-slim | transitive | 4.1.2 | 4.2.4 | 4.2.4 | minor/patch | upgraded |
| fastmcp-tasks | direct | 4.1.2 | 4.2.4 | 4.2.4 | minor/patch | upgraded |
| filelock | direct | 4.2.4 | 4.2.12 | 4.2.12 | minor/patch | upgraded |
| fonttools | transitive | 4.61.1 | 4.68.0 | 4.68.0 | minor/patch | upgraded |
| fqdn | transitive | 1.5.1 | 1.6.0 | 1.6.0 | minor/patch | upgraded |
| github-copilot-sdk | direct | 1.0.29 | 1.0.35 | 1.0.35 | minor/patch | upgraded |
| globus-sdk | direct | 4.6.0 | 4.10.0 | 4.10.0 | minor/patch | upgraded |
| google-api-core | transitive | 2.30.3 | 2.44.0 | 2.44.0 | minor/patch | upgraded |
| google-api-python-client | transitive | 2.201.0 | 2.202.0 | 2.202.0 | minor/patch | upgraded |
| google-auth | transitive | 2.59.1 | 2.63.1 | 2.63.1 | minor/patch | upgraded |
| googleapis-common-protos | transitive | 1.75.5 | 1.76.0 | 1.76.0 | minor/patch | upgraded |
| griffelib | transitive | 2.0.2 | 2.4.1 | 2.4.1 | minor/patch | upgraded |
| h2 | transitive | — | 4.4.1 | 4.4.1 |  | new transitive |
| h5py | direct | 3.15.1 | 3.16.0 | 3.16.0 | minor/patch | upgraded |
| hf-xet | transitive | 1.1.10 | 1.8.0 | 1.8.0 | minor/patch | upgraded |
| hpack | transitive | — | 4.2.0 | 4.2.0 |  | new transitive |
| htmldate | transitive | 1.10.0 | 1.11.0 | 1.11.0 | minor/patch | upgraded |
| httpcore2 | transitive | 2.16.0 | 2.16.2 | 2.16.2 | minor/patch | upgraded |
| httplib2 | transitive | 0.32.0 | 0.32.1 | 0.32.1 | minor/patch | upgraded |
| httpx2 | direct | 2.16.0 | 2.16.2 | 2.16.2 | minor/patch | upgraded |
| huggingface-hub | transitive | 0.35.3 | 2.4.0 | 2.4.0 | **major** | upgraded |
| hyperframe | transitive | — | 6.1.0 | 6.1.0 |  | new transitive |
| hypothesis | direct | 6.156.2 | 6.170.17 | 6.170.17 | minor/patch | upgraded |
| idna | transitive | 3.18 | 3.20 | 3.20 | minor/patch | upgraded |
| importlib-metadata | transitive | 8.7.0 | 8.9.0 | 8.9.0 | minor/patch | upgraded |
| iniconfig | transitive | 2.3.0 | 2.3.1 | 2.3.1 | minor/patch | upgraded |
| iowarp-core | direct | 2.4.1 | 2.5.0 | 2.4.1 | minor/patch | out of scope (moved to 2.5.0 in another session) |
| jaraco-classes | transitive | 3.4.0 | removed | removed |  | dropped from the tree |
| jaraco-context | transitive | 6.1.2 | removed | removed |  | dropped from the tree |
| jaraco-functools | transitive | 4.3.0 | 4.6.0 | removed | minor/patch | dropped from the tree |
| jeepney | transitive | 0.9.0 | removed | removed |  | dropped from the tree |
| jiter | transitive | 0.11.1 | 0.19.0 | 0.19.0 | 0.x minor | upgraded |
| joserfc | transitive | 1.7.4 | 1.8.0 | 1.8.0 | minor/patch | upgraded |
| json-repair | transitive | 0.64.2 | 0.64.9 | 0.64.9 | minor/patch | upgraded |
| jsonpointer | transitive | 3.1.1 | 3.12 | 3.12 | minor/patch | upgraded |
| jsonschema | direct | 4.25.1 | 4.26.0 | 4.26.0 | minor/patch | upgraded |
| jsonschema-path | transitive | 0.3.4 | 0.5.0 | 0.5.0 | 0.x minor | upgraded |
| keyring | transitive | 25.6.0 | 25.7.0 | removed | minor/patch | dropped from the tree |
| kiwisolver | transitive | 1.4.9 | 1.5.1 | 1.5.1 | minor/patch | upgraded |
| librt | transitive | — | 0.17.0 | 0.17.0 |  | new transitive |
| litellm | direct | 1.102.2 | 1.110.2 | 1.110.2 | minor/patch | upgraded |
| lxml | transitive | 6.1.1 | 6.1.3 | 6.1.3 | minor/patch | upgraded |
| lxml-html-clean | transitive | 0.4.5 | 0.4.6 | 0.4.6 | minor/patch | upgraded |
| markdown-it-py | direct | 4.0.0 | 4.3.0 | 4.3.0 | minor/patch | upgraded |
| markupsafe | transitive | 3.0.3 | 3.0.4 | 3.0.4 | minor/patch | upgraded |
| matplotlib | direct | 3.10.8 | 3.11.2 | 3.11.2 | minor/patch | upgraded |
| mcp | direct | 2.2.0 | 2.3.0 | 2.3.0 | minor/patch | upgraded |
| mcp-types | transitive | 2.2.0 | 2.3.0 | 2.3.0 | minor/patch | upgraded |
| more-itertools | transitive | 10.8.0 | 11.2.1 | removed | **major** | dropped from the tree |
| mypy | direct | 1.18.2 | 2.4.1 | 2.4.1 | **major** | upgraded |
| mypy-extensions | transitive | 1.1.0 | 1.2.0 | 1.2.0 | minor/patch | upgraded |
| numcodecs | transitive | 0.17.0 | 0.17.1 | 0.17.1 | minor/patch | upgraded |
| numpy | direct | 2.3.4 | 2.5.5 | 2.5.5 | minor/patch | upgraded |
| oauthlib | transitive | 4.0.0 | 4.0.1 | 4.0.1 | minor/patch | upgraded |
| omegaconf | transitive | 2.3.1 | 2.4.0 | 2.4.0 | minor/patch | upgraded |
| openai | transitive | 2.45.0 | 3.57.0 | 2.54.0 | **major** | held at latest 2.x: openai 3 imports aiohttp at startup (see Decisions) |
| openai-codex-cli-bin | direct | 0.167.0 | 0.171.0 | 0.171.0 | 0.x minor | upgraded |
| opentelemetry-api | transitive | 1.45.1 | 1.45.2 | 1.45.2 | minor/patch | upgraded |
| paramiko | direct | 5.0.0 | 5.1.0 | 5.1.0 | minor/patch | upgraded |
| pathable | transitive | 0.4.4 | 0.6.0 | 0.6.0 | 0.x minor | upgraded |
| pathspec | transitive | 0.12.1 | 1.1.1 | 1.1.1 | **major** | upgraded |
| platformdirs | direct | 4.5.0 | 4.14.8 | 4.14.8 | minor/patch | upgraded |
| playwright | direct | 1.64.1 | 1.65.2 | 1.65.2 | minor/patch | upgraded |
| pluggy | transitive | 1.6.0 | 1.7.0 | 1.7.0 | minor/patch | upgraded |
| prompt-toolkit | direct | 3.0.52 | 3.0.53 | 3.0.53 | minor/patch | upgraded |
| propcache | transitive | 0.4.1 | 0.5.4 | 0.5.4 | 0.x minor | upgraded |
| py-key-value-aio | transitive | 0.4.4 | 0.4.6 | 0.4.6 | minor/patch | upgraded |
| pyarrow | direct | 26.0.0 | 26.0.1 | 26.0.1 | minor/patch | upgraded |
| pyasn1 | transitive | 0.6.4 | 0.6.5 | 0.6.5 | minor/patch | upgraded |
| pycparser | transitive | 2.23 | 3.12 | 3.12 | **major** | upgraded |
| pydantic | direct | 2.12.3 | 2.14.4 | 2.14.4 | minor/patch | upgraded |
| pydantic-core | transitive | 2.41.4 | 2.50.5 | 2.50.5 | minor/patch | upgraded |
| pydantic-settings | transitive | 2.15.0 | 2.16.3 | 2.16.3 | minor/patch | upgraded |
| pydocket | direct | 0.26.2 | 0.31.0 | 0.31.0 | 0.x minor | upgraded |
| pyee | transitive | 14.0.0 | removed | removed |  | dropped from the tree |
| pymongo | transitive | 4.19.0 | 4.20.1 | 4.20.1 | minor/patch | upgraded |
| pyparsing | transitive | 3.3.2 | 3.3.3 | 3.3.3 | minor/patch | upgraded |
| pytest-asyncio | direct | 1.4.1 | 1.5.0 | 1.5.0 | minor/patch | upgraded |
| pytest-cov | direct | 7.0.0 | 7.1.0 | 7.1.0 | minor/patch | upgraded |
| python-json-logger | transitive | 4.1.0 | 4.2.0 | 4.2.0 | minor/patch | upgraded |
| python-multipart | transitive | 0.0.32 | 0.0.34 | 0.0.34 | minor/patch | upgraded |
| pytz | transitive | 2026.3.post1 | 2026.6 | 2026.6 | calver | upgraded |
| pywin32 | transitive | 311 | 312 | 312 | calver | upgraded |
| pywin32-ctypes | transitive | 0.2.3 | removed | removed |  | dropped from the tree |
| pyyaml | direct | 6.0.3 | 6.0.4 | 6.0.4 | minor/patch | upgraded |
| rasterio | direct | 1.4.4 | 1.5.2 | 1.5.2 | minor/patch | upgraded |
| referencing | transitive | 0.36.2 | 0.37.0 | 0.37.0 | 0.x minor | upgraded |
| regex | transitive | 2025.9.18 | 2026.11.19 | 2026.11.19 | calver | upgraded |
| rich | direct | 14.2.0 | 15.0.0 | 15.0.0 | **major** | upgraded |
| rich-rst | transitive | 1.3.2 | 2.2.0 | 2.2.0 | **major** | upgraded |
| rpds-py | transitive | 0.27.1 | 2026.9.1 | 2026.9.1 | **major** | upgraded |
| ruff | direct | 0.14.1 | 0.17.6 | 0.17.6 | 0.x minor | upgraded |
| s3transfer | transitive | 0.19.2 | 0.19.3 | 0.19.3 | minor/patch | upgraded |
| scipy | direct | 1.16.3 | 1.18.1 | 1.18.1 | minor/patch | upgraded |
| secretstorage | transitive | 3.4.0 | 3.5.0 | removed | minor/patch | dropped from the tree |
| sse-starlette | direct | 3.6.0 | 3.7.0 | 3.7.0 | minor/patch | upgraded |
| tenacity | transitive | 9.1.2 | 9.2.1 | 9.2.1 | minor/patch | upgraded |
| tifffile | transitive | 2026.10.15 | 2026.11.16 | 2026.11.16 | calver | upgraded |
| tiktoken | transitive | 0.12.0 | 0.14.0 | 0.14.0 | 0.x minor | upgraded |
| tld | transitive | 0.13.2 | removed | removed |  | dropped from the tree |
| tokenizers | transitive | 0.22.1 | 0.23.3 | 0.23.3 | 0.x minor | upgraded |
| tqdm | transitive | 4.67.1 | 4.70.1 | 4.70.1 | minor/patch | upgraded |
| trafilatura | direct | 2.1.0 | 2.3.1 | 2.3.1 | minor/patch | upgraded |
| truststore | direct | 0.10.4 | 0.10.5 | 0.10.5 | minor/patch | upgraded |
| typer | transitive | 0.27.0 | 0.29.2 | 0.29.2 | 0.x minor | upgraded |
| typing-extensions | transitive | 4.15.0,4.16.0 | 4.16.0 | 4.16.0 | minor/patch | upgraded |
| typing-inspection | transitive | 0.4.2 | 0.4.5 | 0.4.5 | minor/patch | upgraded |
| tzdata | direct | 2026.3 | 2026.6 | 2026.6 | calver | upgraded |
| tzlocal | direct | 5.4.4 | 5.4.5 | 5.4.5 | minor/patch | upgraded |
| uncalled-for | transitive | 0.4.0 | 0.6.2 | 0.6.2 | 0.x minor | upgraded |
| uv | direct | 0.13.6 | 0.13.9 | 0.13.9 | minor/patch | upgraded |
| watchfiles | transitive | 1.1.1 | 1.3.0 | 1.3.0 | minor/patch | upgraded |
| wcwidth | transitive | 0.2.14 | 0.9.5 | 0.9.5 | 0.x minor | upgraded |
| websockets | direct | 15.0.1 | 17.4 | 17.4 | **major** | upgraded |
| xlsxwriter | transitive | 3.2.9 | 3.3.3 | 3.3.3 | minor/patch | upgraded |
| zarr | direct | 3.4.0 | 3.4.2 | 3.4.2 | minor/patch | upgraded |
| zipp | transitive | 3.23.0 | 4.1.2 | 4.1.2 | **major** | upgraded |
