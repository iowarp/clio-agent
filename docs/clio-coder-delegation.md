# CLIO and Clio Coder delegation

With Clio Coder 0.7.0 or newer on `PATH`, the standard CLIO agent's `coding`
plugin can delegate changes without additional model configuration. Select a
model in the CLIO conversation, open a Git workspace, and ask for the change.
The child inherits the conversation's model and cannot exceed its autonomy.
It works in a separate worktree and scoped home. A successful child exit is not
acceptance: CLIO's own check must pass before applying the returned change.

The doctor row checks the **binary**, not provider credentials or model
availability. If the executable is absent, install Clio Coder and restart CLIO.
`CLIO_AGENT_CLIO_CODER_BIN` overrides PATH and may name an executable or a built
JavaScript entry point. A broken override is a configuration error, not an
absent optional integration. Correct or unset it. For an unreadable version,
run the selected entry with `--version` and check its Node.js runtime.

Model precedence is per-call override, `fleet.agents.clio-coder` binding,
`fleet.default`, then the conversation's model. An operator profile via
`CLIO_AGENT_CLIO_CODER_HOME_TEMPLATE` must contain `config/settings.yaml`.
Unset that variable to return to inherited routing. API keys belong in the
provider's environment or credential store, never in task text or CLI arguments.

On non-Windows hosts, the Core loopback probe treats TCP timeout as not running,
as well as connection refusal. This lets WSL self-spawn Core when a closed port
drops rather than refuses the connection. A saturated listener can also time
out; the probe alone does not distinguish it from an absent listener. Socket
resource/permission errors and Windows listener-table failures remain errors,
not permission to replace a daemon.

## One-command golden checks

From the checkout, with dependencies and the pinned marketplace installed:

```sh
export TMPDIR=/path/to/owned/tmp
mkdir -p "$TMPDIR"
uv run --frozen --no-sync python -B scripts/clio_coder_golden.py \
  --out /path/to/owned/new-golden-run
```

The offline checks print PASS or FAIL for Mode A's checked hand-back and apply,
Mode C multi-turn, autonomy ceiling refusal, local and catalog model routing,
and cancellation. They use scripted children and do not establish that a real
parent chooses delegation. The ordinary repository test bootstrap also starts
its private Core daemon. `--protocol-only` omits the global native Core fixtures
for an explicitly protocol-only check; this is not full server qualification.
Any failed, missing or skipped required case prevents success.

## Live candidate run

Start a dedicated CLIO server with an isolated CLIO home, the standard agent,
and a configured model. Give the server access only to the disposable demo
workspace and use a separately prepared Clio Coder home template. Do not point
the demo at a production server or a real user's home.

In the candidate test phase, run:

```sh
uv run --frozen --no-sync python -B scripts/clio_coder_golden.py \
  --live --tc TC-1 --server http://127.0.0.1:18871 \
  --home-template /path/to/owned/coder-home \
  --target litellm --model mini/qwen3.8-27b-iq4r-v4 \
  --runs 5 --out /path/to/owned/new-live-run
```

The script sends a plain request about an addition bug. It requires an actual
`run_clio_coder` result with a successful outcome, passing parent check and
commit, and checks the parent checkout independently. A prose claim or a direct
parent edit without a delegation result does not pass. This demo selects
bypassed approvals for its new, disposable conversation; the child inherits
that ceiling. Server authentication, if enabled, comes from
`CLIO_GACT_BEARER_TOKEN` or `CLIO_AUTH_TOKEN`, not command-line arguments.

Mode C is public: `run_clio_coder(continue_run=...)` takes another turn of a
run, and `land_clio_coder(run=..., action='apply'|'discard')` lands or drops
it. **The golden script does not drive that entry point yet.** Its Mode C case
is still the offline multi-turn stub, and the live script reports Mode C as FAIL
until the stub is replaced. Direct runtime measurements currently use Git apply
only in the demo's own scratch repository.

## Reading the measurements

`candidate.json` records the TC label and clio-agent commit. `latency.json`
records each direct-runtime sample, its actual outcome and phase seconds:
binary resolution, scoped home, worktree, capability probe, spawn, boot to
session, session to first visible token, parent check, hand-back and apply.
Full headless JSON is requested solely to observe text/thinking deltas.
The product's normal terminal JSON stream does not expose first-token timing.
These are observed stream times, not provider-internal token timestamps.
Boot-to-session includes spawn, so those columns must not be added together.
Capability probes retain their normal mtime-based cache: the first sample may
be cold while later samples are warm.

`clio_decides` is null for direct-runtime samples. The separate plain-request
result gives end-to-end parent time, not an invented model-decision phase.
Missing measurements stay null; failed or canceled runs are not performance
wins. Compare the same TC, model, task and cold/warm conditions before claiming
a gain. Collect local and cloud samples separately with approved isolated
provider homes.

The output directory must be new. It retains fixture repositories, scoped
homes and evidence for inspection. Direct-runtime child processes and temporary
worktrees are retired; a timed-out parent turn is canceled. The supplied server
and its conversation state remain owned by the operator. Stop that isolated
server before deleting its demo state or output directory.
