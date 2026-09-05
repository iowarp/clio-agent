# Provider runtime campaign handoff (#1319–#1323)

Status: implemented, qualified locally, reviewed, and committed on the campaign
branches. The paired Agent and Kit packages have not been released.
The released Agent installer continues to install CLIO Kit 2.10.6, whose MCP
runtime behavior is legacy. The shared runtime requires the paired Kit release;
the local evidence below used an explicit candidate-wheel override.

## #1319 / #1321: shared MCP dependency runtime

Normal discovery launches NDP, Geo, Pandas, and Plot as four separate MCP
processes from one persistent `clio-kit[science]` installation. It does not
solve dependencies, build or hash source environments, or run cache collection.
`--isolated` retains the explicit legacy path. Dependency identity is separate
from tool source identity, and live legacy environments remain protected from
collection.

The Kit root gate passed **185 tests**. The candidate installation resolved and
installed **118 packages** once; all four namespaces completed real MCP
discovery from the same interpreter, real CSV profile and line-plot calls
succeeded, and repeated discovery grew the legacy environment cache by **0**.
The production Agent boundary integration
`tests/test_gact/test_hpc_mcp_integration.py` passed **1 test in 68.49 seconds**
using the candidate launcher. It mounted all four namespaces, invoked the real
profile and plot tools, validated the PNG, and reloaded durable HTTP lineage.

See the [Agent integration guide](shared-mcp-runtime-integration-1319.md) and the
Kit [qualification report](https://github.com/iowarp/clio-kit/blob/codex/shared-mcp-runtime/docs/shared-runtime-validation.md).

## #1320: external-input provenance

Recognized external inputs now produce durable `used` edges with schema-argument
evidence without reading, hashing, copying, or minting the source. Output-free
reads remain queryable, graph caps preserve valid endpoints, and recording
failures emit a durable incomplete-provenance diagnostic without changing the
tool result.

Verification passed **18 focused tests** and **298 affected backend tests**.
The UI passed TypeScript checking, **91 component tests**, and **1 Chromium
component/API-fixture test** after a production build. That browser result is
not an installed CLIO/CTE UI qualification. The production Agent/MCP integration
above additionally proved real profile/plot inputs and durable HTTP lineage.

See the [external-input provenance report](external-input-provenance-1320.md).

## #1322 / #1323: provider routing and output caps

The implemented contract binds repair and summary inference to the effective
owning LM, removes the arbitrary vLLM fallback model, fails unknown providers,
omits the client output cap when its configured value is zero, preserves a
positive cap, and turns provider length termination into a typed truncation
error after history and usage recording. The original live Granite-to-Qwen leak
was not reproduced.

Routing gates passed **150 + 33 + 2 tests**. The repair follow-up also passed a
**30-test** focused gate and a **3-test** production/isolation gate. Review is
complete: retry temperatures now increase from the effective rejected call,
hooks and matching adapters run on rebuilt retries, and effective model identity
is invocation-scoped, stale-safe, and isolated across concurrent callers. A final
**4-test** exception-path gate additionally proves that an LM factory failure
preserves the original exception while retaining the production repair and
identity regressions. See the [routing report](provider-runtime-routing-1322-1323.md)
for the final contract and call-site audit.

## Local vLLM qualification

The Agent factory made real calls to vLLM 0.28.0+cpu serving the pinned
`Qwen/Qwen2.5-0.5B-Instruct` revision under the unique alias
`clio-local-qualification`. The default `max_tokens=0` case omitted the field and
the explicit `max_tokens=16` case preserved it; both requests returned HTTP 200.
The service was stopped after qualification.

This proves the local WSL2 AVX2 CPU path. It does not qualify the AMD GPU,
drivers, containers, an HPC filesystem, or HPC performance. The retained
D:-owned runtime occupies **6.79 logical GiB**; that is a logical byte count,
not allocated disk usage. See the [vLLM qualification report](vllm-local-qualification-20260904.md).

## Evidence scope

Historical partial rounds in earlier reports remain negative or superseded
evidence and must not be read as final gates. No package was released, no commit
or push was made, and no live HPC timing or allocated-byte saving is claimed.
