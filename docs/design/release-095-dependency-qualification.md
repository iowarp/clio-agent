# 0.9.5 dependency qualification

The release preparation audit found affected packages in the previous lock,
including FastMCP 4.1.0 below Kit's patched minimum. The repair advances the
active installer roots together to FastMCP/FastMCP Tasks 4.1.2, MCP 2.2.0 and
LiteLLM 1.102.2, and updates the affected transitive locks. The uv floor is
0.13.4 for the certificate-bundle fix.

The ASGI cohort also advances to FastAPI 0.162.0, Starlette 1.10.0 and
SSE-Starlette 3.6.0. The older TestClient imported an AnyIO alias that is now
deprecated, which the strict Python 3.14 CI correctly rejected. The asynchronous
fixture loop scope is explicit; warnings-as-errors remains enabled.

The model router's separately installed proxy uses the same LiteLLM 1.102.2
pin as the agent. The existing equality check caught its older 1.102.1 pin.
DSPy test configuration now uses its context manager instead of monkeypatching
dynamic settings, which leaked a fake usage tracker into later tests. Export
cleanup tests exercise both ASGI 2.4 send failures and the older receive-side
disconnect protocol; the latter must yield while waiting for a disconnect.

The initial Windows Python 3.14 audit of all extras and dependency groups
reported 31 affected packages. After the repair, the same audit reports one:
DiskCache 5.6.3. Its output contains two copies of the same advisory. This is
not a clean audit. The intermediate audit excluded the Git-only clio-schemas
dependency. The final 0.9.5 registry audit includes published Schemas 0.6.0 and
Kit 2.11.1: 235 dependency entries, one affected package, no exclusions, no skipped
packages and no advisory suppression. Marker-specific packages on other
interpreters and operating systems need their own qualification.

## Remaining DiskCache advisory

[CVE-2025-69872](https://github.com/advisories/GHSA-w8v5-vhqr-4h9v)
describes unrestricted pickle deserialization when an attacker can write the
cache. Upstream lists no patched release; PyPI still serves 5.6.3.

CLIO's serving LM constructors explicitly pass `cache=False`. The pinned
DSPy 3.4.0 offers an opt-in restricted unpickler; its default is unrestricted.
A controlled probe of that default evaluated the benign arithmetic payload,
so it is not a mitigation. The SDK compatibility check explicitly selects
`restrict_pickle=True` and checks ordinary data and an executable pickle value.
These facts do not repair DiskCache itself, establish
the safety of every cache operation, or authorize sharing caches with untrusted
writers. The dependency advisory remains recorded, with no audit suppression.

The full release still requires native Tasks compatibility, complete CI, the
live model/Core memory gate and published installer qualification. Findings
and release progress are tracked in
[CLIO #1887](https://github.com/iowarp/clio-agent/issues/1887).
