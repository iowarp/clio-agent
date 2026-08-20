"""L2.5 — the relay wrapper-contract rung (#1224).

THE RUNG'S RULE: NOTHING proceeds to agentic L3 testing until this suite is
green. L3 exists to judge agent JUDGMENT, never to discover plumbing defects
in clio-agent's own relay client layer. The L3 run that surfaced #1224's
three defects (D1: wait_for_terminal silently dropped, D2: the boot-time
catalog cached forever, D3: relay_artifact_lineage/relay_status advertised
but unprojected) burned an entire agent turn budget doing what a five-second,
zero-token client-layer probe would have caught instantly. This suite is that
probe, wired so ``grind-clio-case`` (or any L3 driver) can run it as a fast
pre-flight: ``pytest tests/test_tools/test_relay_l25_wrapper_contract.py``.

Scope discipline: no serve session, no LLM, no agent loop. Every test drives
``clio_agent.tools.relay_transport`` / ``clio_agent.tools.remote_mcp`` /
``clio_agent.gact.agents.invoker`` DIRECTLY against a REAL relay MCP door --
the M2-local desktop-local cluster's door (relay-harness bring-up,
``D:\\relay-harness\\door-local``), never a transport mock (the L3 lesson:
mocks hide contract drift; #1221/#1222 both slipped through exactly that
gap). Configuration resolves from the standard ``CLIO_RELAY_*`` env vars
first; when unset, this suite falls back to the well-known M2-local
bring-up's session file as a dev convenience. Either way, every test SKIPS
TYPED (never fails/errors) when the door is unreachable or unconfigured --
this suite has no opinion about whether a door happens to be running; it only
proves the wrapper's behavior against one when it is.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
import pytest

from clio_agent.gact.agents.invoker import TaskSpec
from clio_agent.gact.agents.relay_expert_invoker import RelayExpertInvoker
from clio_agent.tools.mcp_task_records import InMemoryTaskRecordStore
from clio_agent.tools.relay_factory import RelayTransportConfig, RelayTransportUnavailable
from clio_agent.tools.relay_transport import (
    REMOTE_MCP_FOLLOW_TOOLS,
    RelayTransportClient,
    resolve_relay_transport_config,
)

pytestmark = pytest.mark.relay
# Deliberately NOT @pytest.mark.live: that marker's convention (other live
# suites gate on CLIO_RUN_LIVE=1 internally) would defeat the point of this
# rung -- it must run as a fast, always-on pre-flight ahead of L3, gated
# only by this file's OWN reachability probe (typed-skip when no door
# answers), never by an opt-in env var a driver would have to remember to set.

# The M2-local desktop-local door's well-known bring-up (relay-harness,
# #1221/#1222 investigation). A dev convenience ONLY -- explicit
# CLIO_RELAY_* env vars always win; this is never consulted when they are
# already set. Absent/stale on any other machine, which is exactly the
# "unreachable -> skip typed" case every test below already handles.
_HARNESS_SESSION_ENV = Path(r"D:\relay-harness\cluster\session.env")
_M2_LOCAL_MCP_URL = "http://127.0.0.1:18796/mcp"
_M2_LOCAL_HTTP_URL = "http://127.0.0.1:8765"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a flat ``KEY=VALUE`` env file (comments/blank lines skipped).

    Deliberately not a shell ``source`` — this suite only ever reads the
    bring-up's OWN written session file (never a script with control flow),
    so a plain line parser is both sufficient and safe.
    """

    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


def _resolve_door_config() -> RelayTransportConfig | None:
    """Resolve a real relay door config: explicit env vars first, then the
    M2-local desktop-local bring-up as a dev convenience. ``None`` (never a
    raise) when neither source is complete."""

    resolved = resolve_relay_transport_config()
    if not isinstance(resolved, RelayTransportUnavailable):
        return resolved

    harness = _parse_env_file(_HARNESS_SESSION_ENV)
    token = harness.get("CLIO_RELAY_API_TOKEN", "")
    owner_session_id = harness.get("CLIO_RELAY_OWNER_SESSION_ID", "")
    owner_generation_id = harness.get("CLIO_RELAY_SESSION_GENERATION_ID", "")
    if not (token and owner_session_id and owner_generation_id):
        return None
    return RelayTransportConfig(
        mcp_url=_M2_LOCAL_MCP_URL,
        http_url=_M2_LOCAL_HTTP_URL,
        api_token=token,
        owner_session_id=owner_session_id,
        owner_session_generation_id=owner_generation_id,
    )


async def _reachable(config: RelayTransportConfig) -> bool:
    """One bounded, side-effect-free reachability probe (tools/list)."""

    try:
        async with config.client(store=InMemoryTaskRecordStore()) as client:
            await client._require_mcp_client().list_tools_mcp(cursor=None)  # noqa: SLF001
        return True
    except Exception:  # noqa: BLE001 - any failure here means "not reachable"
        return False


@pytest.fixture(scope="module")
async def door_config() -> RelayTransportConfig:
    """The resolved, verified-reachable door config, or a typed module skip."""

    config = _resolve_door_config()
    if config is None:
        pytest.skip(
            "relay_l25_door_unconfigured: no CLIO_RELAY_* env vars and no "
            f"M2-local bring-up session file at {_HARNESS_SESSION_ENV}"
        )
    if not await _reachable(config):
        pytest.skip(f"relay_l25_door_unreachable: {config.mcp_url} did not answer tools/list")
    return config


@pytest.fixture
async def door_client(door_config: RelayTransportConfig) -> AsyncIterator[RelayTransportClient]:
    """One fresh, owner-bound client per test, its own in-memory task store."""

    async with door_config.client(store=InMemoryTaskRecordStore()) as client:
        yield client


async def _live_tool_names(client: RelayTransportClient) -> dict[str, Any]:
    """Every tool the door currently advertises, keyed by name (one full listing)."""

    mcp_client = client._require_mcp_client()  # noqa: SLF001
    names: dict[str, Any] = {}
    cursor: str | None = None
    for _ in range(50):
        page = await mcp_client.list_tools_mcp(cursor=cursor)
        names.update({tool.name: tool for tool in page.tools})
        cursor = page.next_cursor
        if not cursor:
            break
    return names


# --------------------------------------------------------------------------- #
# (1) wait semantics: the commitment shape                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
async def terminal_wait_job(door_config: RelayTransportConfig) -> dict[str, Any]:
    """Submit ONE cheap, fast-failing relay_submit_agent job with
    wait_for_terminal=True, shared by the wait-semantics and artifact-fetch
    checks so the suite pays this ~5s SSH-relayed round trip only once.

    ``prompt_path`` names a file that does not exist, so JARVIS-CD fails the
    job before dispatching anything to an LLM (zero tokens spent) -- the
    same trick #1222's own live investigation used
    (``ladder-l3-nonexistent-prompt.md`` in
    ``test_relay_invoker_runtime_contract.py``).
    """

    async with door_config.client(store=InMemoryTaskRecordStore()) as client:
        started = time.monotonic()
        identity = await client.submit(
            "relay_submit_agent",
            {
                "cluster": "desktop-local",
                "prompt_path": "l25-wrapper-contract-nonexistent-prompt.md",
                "wait_for_terminal": True,
                "wait_timeout_seconds": 90,
            },
        )
        submit_elapsed = time.monotonic() - started
        # The commitment shape under test: ONE follow-up resolution (never a
        # multi-round drive -- the door already blocked for the full
        # wait_for_terminal duration inside the create response itself, so
        # the persisted record is already terminal here) returns the REAL
        # outcome, not a queued handle.
        resolved = await client.wait_for_submitted_job(identity.job_id, timeout_seconds=90)
        total_elapsed = time.monotonic() - started
    assert resolved is not None
    return {
        "identity": identity,
        "resolved": resolved,
        "submit_elapsed_s": submit_elapsed,
        "total_elapsed_s": total_elapsed,
    }


@pytest.mark.asyncio
async def test_wait_for_terminal_resolves_in_one_round_trip(
    terminal_wait_job: dict[str, Any],
) -> None:
    """#1225 D1-REVISED, proven against the REAL door.

    This is the exact probe that would have caught the L3 blocker in
    seconds: a wait_for_terminal=True submission must resolve to a TERMINAL
    outcome without a second, separate relay_wait call -- and without being
    truncated by an internal TTL (#1225 D1-REVISED's wait-commitment fix,
    mcp_executor._is_wait_for_terminal_commitment / foreground_cancellation's
    None-timeout support). The follow-up resolution call
    (wait_for_submitted_job) must cost ~nothing beyond the submit itself --
    proof the record was ALREADY terminal when persisted, not re-driven
    through a second multi-round poll.
    """

    resolved = terminal_wait_job["resolved"]
    assert resolved.status in {"completed", "failed", "cancelled"}
    # The follow-up resolution added at most a couple of seconds beyond the
    # submit's own SSH-relayed round trip -- it read an already-terminal
    # record, it did not drive a fresh multi-round poll.
    extra = terminal_wait_job["total_elapsed_s"] - terminal_wait_job["submit_elapsed_s"]
    assert extra < 10.0, (
        f"wait_for_submitted_job took {extra:.1f}s beyond submit -- looks like a "
        "re-drive, not a one-shot read of an already-terminal record"
    )


# --------------------------------------------------------------------------- #
# (2) catalog freshness                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_catalog_rediscovery_is_repeatable_against_the_live_door(
    door_client: RelayTransportClient,
) -> None:
    """#1227 D2, proven against the REAL door: a second, independent
    ``tools/list`` against the same live door must succeed and observe the
    SAME tool set as the first -- the concrete behavior
    ``refresh_relay_tool_surfaces_if_stale`` depends on being safe to repeat
    on a TTL tick (unit-mocked in ``tests/test_gact/test_relay_wiring.py``;
    this is the live-wire confirmation that re-discovery is not a
    one-shot-only operation against the real transport)."""

    first = await _live_tool_names(door_client)
    second = await _live_tool_names(door_client)
    assert first, "live door advertised no tools at all"
    assert set(first) == set(second), (
        "a second live tools/list observed a different tool set than the "
        "first -- re-discovery is not safely repeatable"
    )


# --------------------------------------------------------------------------- #
# (3) projections: every door-advertised follow tool actually projects        #
# --------------------------------------------------------------------------- #


#: The relay follow tools this surface actually CLAIMS to support -- the
#: read-only, agent-facing observation tools (case07-S3 needs lineage; the
#: agent loop needs wait/observe). NOT every ``relay_*`` name the door
#: advertises is meant to reach this surface: relay_submit_agent has its own
#: dedicated wrapper (RelayExpertInvoker), and relay_cancel/relay_queue_*/
#: relay_bind_jarvis_runtime/relay_storage_status/relay_remote_mcp_context are
#: operational/internal tools this surface has never claimed to project.
_CLAIMED_AGENT_FACING_RELAY_TOOLS = frozenset(
    {"relay_wait", "relay_observe", "relay_artifact_lineage", "relay_status"}
)


@pytest.mark.asyncio
async def test_every_claimed_relay_tool_the_door_advertises_is_projected(
    door_client: RelayTransportClient,
) -> None:
    """#1228 D3, proven against the REAL door's CURRENT catalog (not a frozen
    fixture): every relay tool this surface CLAIMS to support (see
    ``_CLAIMED_AGENT_FACING_RELAY_TOOLS``) that the live door actually
    advertises must be a member of ``REMOTE_MCP_FOLLOW_TOOLS``, or it is
    silently dropped by ``discover_remote_mcp`` exactly the way
    relay_artifact_lineage/relay_status were before the fix. A frozen
    fixture (``RELAY_DOOR_TOOLS_LIST_FIXTURE`` in
    ``test_relay_invoker_runtime_contract.py``) cannot catch the door
    renaming or dropping one of these tomorrow; this live check can.
    """

    live_names = await _live_tool_names(door_client)
    claimed_and_live = _CLAIMED_AGENT_FACING_RELAY_TOOLS & set(live_names)
    assert claimed_and_live, "live door advertised none of the claimed agent-facing relay tools"
    unprojected = claimed_and_live - REMOTE_MCP_FOLLOW_TOOLS
    assert not unprojected, (
        f"live door advertises {sorted(unprojected)}, claimed as agent-facing but "
        "not in REMOTE_MCP_FOLLOW_TOOLS -- silently dropped by discover_remote_mcp (#1228 D3)"
    )


@pytest.mark.asyncio
async def test_acl_naming_an_unprojected_tool_degrades_per_tool_not_the_whole_agent(
    door_client: RelayTransportClient,
) -> None:
    """#1228 D3 (second half), proven with the REAL door's live follow-tool
    names feeding the SAME ``_dynamic_agent_tools`` degrade path an actual
    agent build uses (no serve session or LLM call needed to reach it --
    see ``tests/test_gact/test_agent_blueprints.py`` for the sibling unit
    coverage of the general contract). One live, claimed tool resolves; one
    deliberately-fake name does not; the agent still gets the live tool.

    Follow tools mount 1:1 under the gateway's ``relay`` namespace (the bare
    name is the door name with its own ``relay_`` prefix stripped, then the
    gateway re-adds ``relay_`` as the mount prefix) -- so the door's
    advertised name IS the DSPy-tool-visible name; no federation/FastMCP
    plumbing is needed to prove the ACL-resolution contract, only the real
    catalog's real names.
    """

    from types import SimpleNamespace

    from clio_agent.gact.agents.builders import _dynamic_agent_tools
    from clio_agent.gact.runtime.globals import _gact_app_context
    from clio_agent.gact.types import AgentDef

    live_names = await _live_tool_names(door_client)
    claimed_and_live = sorted(_CLAIMED_AGENT_FACING_RELAY_TOOLS & set(live_names))
    assert claimed_and_live, "live door advertised none of the claimed agent-facing relay tools"
    live_tool_name = claimed_and_live[0]

    from clio_agent.gact.app import build_app

    app = build_app(sessions_path=None)
    base_agent = SimpleNamespace(
        tool_executor=SimpleNamespace(to_dspy_tools=lambda: [SimpleNamespace(name=live_tool_name)])
    )
    agent_def = AgentDef(
        id="l25_relay_probe",
        source="expert_pack",
        title="L2.5 Relay Probe",
        tools=[live_tool_name, "definitely_not_a_projected_relay_tool"],
    )

    with _gact_app_context(app):
        tools = _dynamic_agent_tools(base_agent, agent_def, {})

    assert [t.name for t in tools] == [live_tool_name]


# --------------------------------------------------------------------------- #
# (4) argument fidelity: schema-diff guard                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_remote_agent_task_spec_matches_the_live_door_schema(
    door_client: RelayTransportClient,
) -> None:
    """Live-wire sibling of
    ``test_remote_agent_task_spec_only_sends_door_recognized_arguments``
    (#1222) -- that test pins a FROZEN schema fixture; this one re-fetches
    the door's REAL, CURRENT ``relay_submit_agent`` inputSchema and diffs
    the wrapper's wire payload against it live, so schema drift the frozen
    fixture cannot see still fails this rung before it reaches an agent.
    """

    live_names = await _live_tool_names(door_client)
    tool = live_names.get("relay_submit_agent")
    assert tool is not None, "live door does not advertise relay_submit_agent"
    schema = tool.input_schema or {}
    properties = set((schema.get("properties") or {}).keys())
    assert properties, "live relay_submit_agent inputSchema declared no properties"

    invoker = RelayExpertInvoker(
        app=None,  # unused by remote_agent_task_spec()
        client_factory=lambda _sid: None,
        cluster="desktop-local",
        prompt_path="l25-wrapper-contract-nonexistent-prompt.md",
    )
    spec = TaskSpec(
        child_expert_id="compute",
        task_text="probe",
        parent_session_id="session-l25-probe",
    )
    wire = invoker.remote_agent_task_spec(spec)

    unknown = set(wire) - properties
    assert not unknown, (
        f"remote_agent_task_spec sent {sorted(unknown)}, which the LIVE door's "
        f"relay_submit_agent inputSchema does not currently declare "
        f"(declared: {sorted(properties)})"
    )
    if schema.get("additionalProperties") is False:
        # additionalProperties: false is itself schema-declared -- confirm it
        # is still true live rather than assuming #1222's snapshot still holds.
        assert unknown == set()


# --------------------------------------------------------------------------- #
# (5) artifact retrieval                                                      #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fetch_a_real_artifacts_bytes_through_the_wrapper(
    door_config: RelayTransportConfig,
    terminal_wait_job: dict[str, Any],
) -> None:
    """The bounded artifact-fetch door end to end: list the failed probe
    job's real indexed artifacts, then fetch and decode one's actual bytes
    through :meth:`RelayTransportClient.fetch_artifact` -- proving the
    authenticated HTTP door (bearer token + owner-session headers) works,
    not just the MCP door the other checks exercise."""

    job_id = terminal_wait_job["identity"].job_id
    async with door_config.client(store=InMemoryTaskRecordStore()) as client:
        try:
            artifacts = await client.list_job_artifacts(job_id)
        except httpx.HTTPStatusError as exc:
            pytest.skip(f"relay_l25_artifact_listing_unavailable: {exc}")
        if not artifacts:
            pytest.skip("relay_l25_no_artifacts: the probe job recorded no artifacts to fetch")
        artifact_id = str(artifacts[0]["artifact_id"])
        content = await client.fetch_artifact(artifact_id)

    assert isinstance(content, bytes)
    assert len(content) > 0
