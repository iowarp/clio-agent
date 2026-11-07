# GitHub Copilot provider

Use the models included with your GitHub Copilot plan as a CLIO LM provider,
signed in with CLIO's own GitHub account.

CLIO drives the [GitHub Copilot SDK](https://github.com/github/copilot-sdk)
(`github-copilot-sdk`) as a **model transport only**. When
`CLIO_LM_PROVIDER=copilot`, `create_lm` builds
`dspy.LM("copilot/<model>", engine=CopilotEngine(...), async_engine=AsyncCopilotEngine(...))`
(no LiteLLM), and the engine sends each call through one pooled Copilot runtime
per GACT session (`src/clio_agent/providers/copilot/`).

## What CLIO switches off

Every Copilot agent feature that could act on the machine or read ambient state
is disabled explicitly in `providers/copilot/options.py`, and
`tests/test_providers/test_copilot_options.py` pins each flag:

- no built-in, custom or MCP tools (`available_tools=[]`), no skills, memory,
  hooks, custom instructions or config discovery;
- no git operations, session store, autopilot continuation or infinite-session
  compaction;
- the client runs in `empty` mode under a CLIO-owned `COPILOT_HOME`
  (`<user data>/copilot/home`), with telemetry off;
- `use_logged_in_user=False`: the machine's own `copilot login` is never read.

CLIO's agent loop (`ClioReAct`) and its MCP/tool gateway stay the only tool
path. The engine renders the request's tools as text
(`lm/engines/text_tools.py`), and the model ends a message with one fenced
`tool_calls` block, which comes back as typed tool calls. Images and PDFs are
sent as inline `blob` attachments.

## Sign-in: one GitHub account for sources and Copilot

Copilot does not have its own login in CLIO. It uses the GitHub account CLIO
already signs in to for GitHub sources (the `clio-agent` GitHub App device flow;
the token lives in CLIO's credential vault). Signing in from the Copilot row and
signing in from **Settings › Sources › GitHub** are the same sign-in:

- The Copilot row's **Sign in** starts the GitHub device flow when CLIO is not
  signed in to GitHub yet, and reuses the existing sign-in when it is.
- The provider reads the vault token for the client's `github_token` and
  answers the runtime's per-session `github_token_provider` callback from it,
  so a refreshed token reaches a long-lived session without a restart.
- Signing out of GitHub (Settings › Sources) releases every Copilot runtime;
  signing in to a different account marks the model list stale
  (`overlay_account_changed`) so it is listed again for the new seat.

## Setup (Desktop or Web)

Prerequisites outside CLIO:

- A GitHub account with a Copilot seat (Free, Pro, Pro+, Business or
  Enterprise).
- For Business/Enterprise seats, the organization must allow **Copilot CLI**
  (the SDK runs the Copilot CLI runtime). An org policy that disables it makes
  every model unavailable for that seat.
- The `clio-agent` GitHub App must request the **Copilot Requests** account
  permission. Accept the App's updated permissions once if GitHub asks.

Then, in the model picker or Settings › Providers:

1. **Install** on the GitHub Copilot row. CLIO installs the `copilot` extra
   (`github-copilot-sdk`) and stages its pinned runtime
   (`python -m copilot download-runtime`). A turn never downloads anything.
2. **Sign in**. Enter the code GitHub shows, or reuse CLIO's existing GitHub
   sign-in.
3. **Check** the provider. CLIO lists the models your seat and org policy
   allow from the runtime's live `list_models()`. There is no bundled list.
   Models a policy disables are recorded as rejected rows, not silently hidden.
4. Pick a model and send a turn.

## Headless

```powershell
$env:CLIO_LM_PROVIDER='copilot'
$env:CLIO_LM_MODEL='<a model id from the Copilot row>'
uv run clio-agent serve --host 127.0.0.1 --port 8100
```

The sign-in still comes from CLIO's GitHub account in the vault. Sign in once
through Desktop/Web, or through the generic provider sign-in API:
`POST /v1/providers/copilot/auth` with `{"action": "start"}` returns the device
code, then `{"action": "status", "flow_id": "..."}` polls until GitHub confirms
it. There is no environment-variable token, and the machine's `copilot login`
is never used.

## Runtime selection

`github-copilot-sdk` pins a runtime release. CLIO uses your own `copilot` CLI
only when it is a native executable reporting a **newer** version than the
pinned one, otherwise the staged runtime. The provider panel shows which one
runs (installed or bundled) and its version. The SDK and its runtime are user-updatable components: the
provider panel's client update moves them forward between CLIO releases.

## Sessions

Each GACT session gets one Copilot runtime process. Inside an agent loop the
engine keeps one Copilot session per conversation and sends only the new
messages; anything else starts a new session with the whole transcript, with a
typed reset reason on the `provider.stateful` audit row. Any failure burns the
runtime, so the next call starts fresh with a freshly read credential. Idle
runtimes are reaped after `providers.copilot.session_idle_ttl_s`
(`CLIO_COPILOT_SESSION_IDLE_TTL_S`, default 300 s).

## Usage and quota

Premium models draw on the seat's monthly premium-request allowance. CLIO reads
the allowance with `account.getQuota` and shows it in the fleet usage view as a
"Monthly premium" window. An unlimited quota type is left out, and an unreadable
quota keeps the last reading with `copilot_quota_unreadable` rather than showing
zero. Each call's `cost` multiplier and tokens are recorded on the
`provider.call_usage` row. An exhausted allowance raises `copilot_plan_limit`
with the reset time from the last reading.

## Troubleshooting

**"GitHub Copilot needs a GitHub sign-in."** CLIO is not signed in to GitHub,
or the token was revoked. Sign in from the Copilot row or Settings › Sources.

**"The signed-in GitHub account has no GitHub Copilot access."** The account
has no Copilot seat, or the `clio-agent` GitHub App lacks the Copilot Requests
permission.

**No models, or fewer than expected.** The seat's org policy disables Copilot
CLI or specific models. Each model a policy disables is listed under the
refresh's rejected rows with `copilot_policy_disabled` (or
`copilot_policy_unconfigured`).

**"GitHub Copilot support is not installed on the connected agent."** Use
**Install** on the Copilot row. If only the runtime is missing ("The GitHub
Copilot runtime is not staged"), Install stages it again.

**Plan limit reached.** The monthly premium allowance is used up. Pick a model
with a 0x multiplier, or wait for the reset time CLIO shows.
