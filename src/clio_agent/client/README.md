# Running CLIO agents and flows from Python

`pip install clio-agent` ships the `clio` module (the PyPI package named `clio` is
unrelated; always install `clio-agent`). A script runs an agent or a flow and gets
the result back, on a laptop with CLIO open or on a CI runner where nothing runs:

```python
from clio import ClioClient, ClioOptions

options = ClioOptions(workspace=".", agent="factorio")
with ClioClient(options=options) as c:
    result = c.run("Reduce the mass of the bracket in bracket.inp by 30% under the same load case")
    print(result.text)
    for artifact in result.artifacts:
        print(artifact.path)

options = ClioOptions(workspace=".", agent="deep-researcher")
with ClioClient(options=options) as c:
    result = c.run_flow(
        "deep-research",
        "What limits the write bandwidth of node-local NVMe burst buffers at scale?",
        options={"max_rounds": 3},
    )
    print(result.artifacts[0].path)
```

Run it with `python my_script.py`. Nothing asks anyone anything.

## How the client holds a server

- **Attach or start.** Opening the client looks for a CLIO server at `options.url`
  (default: the configured local port, `gact.port` / `CLIO_PORT` / 17800). If one
  answers, the client attaches to it, with everything it has installed and signed in.
  If none answers and `launch` is true (the default), it starts `clio-agent serve` on
  that port, using your CLIO home and the machine's clio-core daemon. With
  `launch=False`, or with a remote URL, no server is an error naming the URL.
- **Last one out stops it.** Every client registers with the server's attached-client
  count. Closing the block stops a server this client started only when no other
  client is attached; otherwise the server keeps running and stops with its last
  client. A server the client attached to is never stopped by it.
- **The banner.** Opening prints one line pair to stderr: attached (and where) or
  started (and on which port), the model provider, that nothing will be asked, and the
  approval mode. Each plugin trusted during a run is printed as it is trusted.
- **Configuration** comes from the environment, as for `clio-agent serve`: set
  `CLIO_LM_PROVIDER` and the provider's key variables (CI secrets, not files).

## Options

| Option | Meaning | Default |
| --- | --- | --- |
| `url` | Where to look for a CLIO server | The configured local port |
| `token` | Bearer token | The server's credential record, then `CLIO_GACT_BEARER_TOKEN` |
| `launch` | Start a local server when none answers | `True` |
| `workspace` | Folder the runs work in (on the server's machine for a remote URL) | `.` |
| `agent` | Agent id (`factorio`) or `marketplace/id` | The server's default agent |
| `model` | `account/model`, as `list_models` gives it | The session default |
| `approval_mode` | `ask`, `auto-edits`, `bypass`, `ai-review`, `spotter-ai` | `bypass` |
| `max_turns` | Most model turns (steps) over the run's session tree | No limit |
| `max_budget_usd` | Most the run may spend over its session tree | No limit |

`run`, `run_flow`, `stream_flow` and `session` override `agent`, `model`,
`approval_mode`, `max_turns` and `max_budget_usd` per call.

## Nothing is asked

A script's sessions are *unattended*:

- Tools run under the approval mode (`bypass`: all of them). A person's explicit deny
  rules still apply. Anything a narrower mode would ask about is denied, typed
  `unattended_no_person`, never left waiting.
- A question (`ask_user`, a flow's `ctx.ask`) is answered at once: the user is not
  available, go ahead on your best judgment. A choice-only question is declined with
  that reply (`ctx.ask(..., schema=bool)` returns `False`).
- Plugins the run needs are trusted, including flows and agents from marketplaces
  other than the default (name such an agent `marketplace/id`). Each is listed in
  `RunResult.trusted`.

## Results

`run` and `run_flow` return a `RunResult`: `status` (`completed`, `failed`,
`cancelled`), `text`, `artifacts` (path, artifact id, version), `session_id`,
`error` (the server's typed error when failed), `trusted`, and `usage` (model turns,
tokens and cost over the session tree, from `GET /v1/sessions/{sid}/usage`). A failed
run returns `status="failed"`; it does not raise. A run that reaches `max_turns` or
`max_budget_usd` fails with `error.error == "run_limit_reached"` and
`error.details["limit"]` naming the limit. Mistakes in using the library raise
(`ServerUnavailable`, `ServerStartFailed`, `ProviderNotConfigured`,
`AgentNotAvailable`), each naming the problem and the fix.

## Sessions and streams

```python
with ClioClient(options=options) as c:
    s = c.session()                     # several runs in one conversation
    first = s.run("Summarize bracket.inp")
    for event in s.stream("Now lighten it"):
        print(type(event).__name__)     # MessagePartDelta, ToolCallStarted, ... then RunResult
    again = c.session(id=first.session_id)   # continue one made earlier, here or in the UI

    for event in c.stream_flow("deep-research", "...", options={"max_rounds": 1}):
        if isinstance(event, TaskStarted):
            print("researcher started:", event.label)
```

`AsyncClioClient` takes the same options for `async with`; its methods are
coroutines and its streams async iterators.

## A flow as a session's own work

`run_flow` creates the session with `flow: {id, options}` (`POST /v1/sessions`). The
server checks it with the same function `run_flow` uses in a turn, so an unknown flow
or options the flow does not declare are refused with the same typed reason
(`flow_not_resolved`, `flow_options_invalid`), returned as a failed result. The first
message is the flow's input; each later message starts a new run. A session's
`agent_flow` cannot be set through `metadata` (`metadata_reserved`).

## The typed API

The same client exposes the GACT v1 API: `sessions`, `messages`, `workspaces`,
`permissions`, `documents`, `health()`, `capabilities()`, with errors mirroring the
§14 taxonomy (`NotFoundError`, `InvalidRequestError`, ... all `ClioAPIError`).
Nothing retries silently: `retries=` (GET transport failures) and
`reconnect_attempts=` (SSE drops) are opt-in, bounded and logged on
`logging.getLogger("clio_agent.client")`.
