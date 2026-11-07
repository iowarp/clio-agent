"""GitHub Copilot as a DSPy 3.4 engine (:mod:`clio_agent.providers.copilot.engine`).

Pins, against the fake SDK (``tests/_fake_copilot_sdk.py``) through the REAL pooled
transport (:mod:`clio_agent.providers.copilot.sessions`):

* the request crosses as text (tools rendered, the reminder appended) with the system
  prompt replacing Copilot's own; images and PDFs ride as inline ``blob`` attachments;
* streaming: reasoning as thinking, text, the fenced tool block as typed calls, usage
  with cache reads and writes; ``finish_reason: length`` is a ``length`` finish;
* one Copilot session per conversation inside an agent loop (a delta send continues
  it); outside a loop every call is a fresh session and the previous one is deleted;
* a ``session.error`` raises its typed CLIO error and burns the runtime; a stall is a
  ``dspy.lm15`` timeout and a dead transport a ``ServerError``;
* the sync twin and the quota read after a call.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from dspy.lm15 import (
    DocumentPart,
    FunctionTool,
    ImagePart,
    Message,
    Request,
    ServerError,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolResultPart,
)
from dspy.lm15 import TimeoutError as LMTimeoutError

from clio_agent.gact import context as gact_context
from clio_agent.lm.engines.text_tools import FENCE, TURN_REMINDER
from clio_agent.providers import stateful_common
from clio_agent.providers.copilot.engine import CopilotEngine, blob_attachments
from clio_agent.providers.copilot.errors import (
    COPILOT_SIGNED_OUT_MESSAGE,
    CopilotAuthRequiredError,
    CopilotNoSeatError,
    CopilotPlanLimitError,
    CopilotSessionError,
)
from clio_agent.providers.copilot.sessions import SESSION_POOL
from clio_agent.providers.usage.capture import COPILOT_ACCOUNT
from clio_agent.providers.usage.snapshot import USAGE_BOARD
from tests import _fake_copilot_sdk as fake

SEARCH = FunctionTool(
    name="search",
    description="Search.",
    parameters={"type": "object", "properties": {"q": {"type": "string"}}},
)
HEAD = Message.user("what?")
BLOCK = f'\n{FENCE}\n[{{"name": "search", "arguments": {{"q": "x"}}}}]\n```'


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The fenced JSON call protocol; an empty pool, registry and usage board."""
    monkeypatch.setenv("CLIO_TEXT_TOOL_PROTOCOL", "json")
    fake.reset()
    USAGE_BOARD.clear()
    yield
    fake.reset()
    USAGE_BOARD.clear()


@pytest.fixture
def loop_scope() -> Iterator[None]:
    session_token = gact_context.set_session_id("sess-a")
    scope_token = gact_context.set_react_scope("main", "react")
    try:
        with stateful_common.stateful_scope():
            yield
    finally:
        gact_context.reset(scope_token)
        gact_context.reset(session_token)


def _request(*messages: Message, system: str = "You are clio.") -> Request:
    return Request(model="copilot/gpt-5", system=system, messages=messages, tools=(SEARCH,))


def _step(i: int) -> tuple[Message, Message]:
    call = ToolCallPart(id=f"c{i}", name="search", input={"q": str(i)})
    return (
        Message.assistant([TextPart(text=f"step {i}"), call]),
        Message(
            role="tool",
            parts=(ToolResultPart(id=f"c{i}", name="search", content=(TextPart(text=f"r{i}"),)),),
        ),
    )


async def test_the_request_crosses_as_text_with_the_system_replacing_copilots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = fake.install(
        monkeypatch,
        script=[
            [
                fake.reasoning("plan"),
                fake.text("Looking." + BLOCK),
                fake.usage(
                    input_tokens=20, output_tokens=5, cache_read_tokens=300, cache_write_tokens=12
                ),
            ]
        ],
    )
    response = await fake.drive(_request(HEAD))

    [(prompt, attachments, _sid)] = sdk.sends()
    assert prompt == f"[user]\nwhat?\n\n{TURN_REMINDER}"
    assert attachments is None
    [session] = sdk.sessions()
    system = session.kwargs["system_message"]
    assert system["mode"] == "replace"
    assert system["content"].startswith("# How you act")
    assert system["content"].endswith("You are clio.")
    parts = response.message.parts
    assert parts[0] == ThinkingPart(text="plan")
    assert parts[1] == TextPart(text="Looking.")
    assert (parts[2].name, parts[2].input) == ("search", {"q": "x"})
    assert response.finish_reason == "tool_call"
    usage = response.usage
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (20, 300, 12)


async def test_a_plain_reply_is_text_and_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch)
    response = await fake.drive(fake.request())
    assert [p.text for p in response.message.parts if isinstance(p, TextPart)] == ["Answer"]
    assert response.finish_reason == "stop"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (2, 3)


async def test_an_assembled_message_without_deltas_is_the_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake.install(
        monkeypatch,
        script=[[fake.event("assistant.message", content="whole", parent_tool_call_id=None)]],
    )
    response = await fake.drive(fake.request())
    assert [p.text for p in response.message.parts if isinstance(p, TextPart)] == ["whole"]


async def test_a_length_finish_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch, script=[fake.answer("cut", finish_reason="length")])
    assert (await fake.drive(fake.request())).finish_reason == "length"


async def test_an_empty_reply_is_a_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch, script=[[fake.usage()]])
    with pytest.raises(ServerError, match="empty content"):
        await fake.drive(fake.request())


async def test_images_and_pdfs_ride_as_inline_blobs(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = fake.install(monkeypatch)
    image = ImagePart(data="aGk=", media_type="image/png")
    pdf = DocumentPart(data="JVBE", media_type="application/pdf")
    await fake.drive(fake.request(Message(role="user", parts=(TextPart(text="look"), image, pdf))))
    [(_prompt, attachments, _sid)] = sdk.sends()
    assert attachments == [
        {"type": "blob", "data": "aGk=", "mimeType": "image/png", "displayName": "image-1"},
        {
            "type": "blob",
            "data": "JVBE",
            "mimeType": "application/pdf",
            "displayName": "document-2",
        },
    ]


def test_a_remote_image_url_is_refused_even_when_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blob is inline: Copilot never fetches a URL on CLIO's behalf."""
    monkeypatch.setattr(
        "clio_agent.providers.claude_code_multimodal.native_image_url_allowlist",
        lambda: frozenset({"cdn.test"}),
    )
    with pytest.raises(ValueError, match="inline attachments only"):
        blob_attachments([ImagePart(url="https://cdn.test/a.png", media_type="image/png")])


async def test_a_refused_attachment_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    sdk = fake.install(monkeypatch)
    bad = ImagePart(data="aGk=", media_type="image/tiff")
    with pytest.raises(ValueError):
        await fake.drive(fake.request(Message(role="user", parts=(TextPart(text="x"), bad))))
    assert sdk.clients == []


async def test_in_a_loop_the_second_call_is_a_delta_on_the_same_session(
    monkeypatch: pytest.MonkeyPatch, loop_scope: None
) -> None:
    sdk = fake.install(monkeypatch)
    await fake.drive(_request(HEAD))
    await fake.drive(_request(HEAD, *_step(0)))

    (first, _a1, sid1), (second, _a2, sid2) = sdk.sends()
    assert first.startswith("[user]\nwhat?")
    assert second.startswith("[tool results]\n[c0 search]\nr0")
    assert sid1 == sid2
    assert len(sdk.clients) == 1 and len(sdk.sessions()) == 1


async def test_outside_a_loop_every_call_is_a_fresh_session_and_the_old_one_is_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = fake.install(monkeypatch)
    await fake.drive(_request(HEAD))
    await fake.drive(_request(HEAD, *_step(0)))

    (_first, _a1, sid1), (second, _a2, sid2) = sdk.sends()
    assert second.startswith("[user]\nwhat?")  # the FULL transcript
    assert sid1 != sid2
    [client] = sdk.clients  # one runtime, two sessions
    assert client.deleted == [sid1]
    assert client.sessions[0].disconnected


async def test_a_compacting_runtime_resets_the_kept_conversation(
    monkeypatch: pytest.MonkeyPatch, loop_scope: None
) -> None:
    sdk = fake.install(
        monkeypatch, script=[[*fake.answer(), fake.event("session.compaction_complete")]]
    )
    await fake.drive(_request(HEAD))
    await fake.drive(_request(HEAD, *_step(0)))
    (_f, _a1, sid1), (second, _a2, sid2) = sdk.sends()
    assert second.startswith("[user]\nwhat?")  # compacted: the whole transcript again
    assert sid1 != sid2


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"remediation": "sign_in"}, CopilotAuthRequiredError),
        ({"status_code": 401}, CopilotAuthRequiredError),
        ({"remediation": "switch_account"}, CopilotNoSeatError),
        ({"error_type": "quota", "status_code": 402}, CopilotPlanLimitError),
        ({"error_type": "server"}, CopilotSessionError),
    ],
)
async def test_a_session_error_raises_its_typed_error_and_burns_the_runtime(
    monkeypatch: pytest.MonkeyPatch, data: dict[str, Any], error: type[Exception]
) -> None:
    sdk = fake.install(monkeypatch, script=[[fake.error(**data)]])
    with pytest.raises(error):
        await fake.drive(fake.request())
    await asyncio.sleep(0.05)
    [client] = sdk.clients
    assert client.stopped
    await fake.drive(fake.request())
    assert len(sdk.clients) == 2  # the next call starts a fresh runtime


async def test_a_stall_is_a_dspy_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch, script=[fake.HANG])
    with pytest.raises(LMTimeoutError, match="idle for 0s"):
        await fake.drive(fake.request(), idle_timeout_s=0.2)


async def test_a_dead_transport_is_a_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch, script=[fake.ProcessExitedError("runtime exited")])
    with pytest.raises(ServerError, match="ProcessExitedError"):
        await fake.drive(fake.request())


async def test_a_signed_out_github_raises_the_typed_sign_in_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk = fake.install(monkeypatch, token=None)
    with pytest.raises(CopilotAuthRequiredError, match=COPILOT_SIGNED_OUT_MESSAGE):
        await fake.drive(fake.request())
    assert sdk.clients == []  # no runtime without CLIO's GitHub sign-in


def test_the_sync_twin_runs_on_its_own_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch)
    response = CopilotEngine("gpt-5", cwd="/w", idle_timeout_s=5.0).complete(fake.request())
    assert [p.text for p in response.message.parts if isinstance(p, TextPart)] == ["Answer"]


async def test_the_quota_is_read_after_a_call_with_clios_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quota = {
        "premium_interactions": {
            "entitlement_requests": 300,
            "used_requests": 75,
            "remaining_percentage": 75.0,
            "is_unlimited_entitlement": False,
            "reset_date": "2026-11-01",
        },
        "chat": {"is_unlimited_entitlement": True},
    }
    sdk = fake.install(monkeypatch, quota=quota)
    await fake.drive(fake.request())
    for _ in range(50):
        if USAGE_BOARD.latest(COPILOT_ACCOUNT) is not None:
            break
        await asyncio.sleep(0.02)
    snapshot = USAGE_BOARD.latest(COPILOT_ACCOUNT)
    assert snapshot is not None
    assert sdk.quota_tokens == [fake.TOKEN]
    [window] = snapshot.windows
    assert (window.name, window.used_percent, window.resets_at) == (
        "Monthly premium",
        25.0,
        "2026-11-01",
    )
    assert snapshot.balance is not None
    assert (snapshot.balance.used, snapshot.balance.limit) == (75.0, 300.0)


async def test_the_pool_counts_one_runtime_per_session(monkeypatch: pytest.MonkeyPatch) -> None:
    fake.install(monkeypatch)
    await fake.drive(fake.request())
    await fake.drive(fake.request())
    assert SESSION_POOL.construction_count == 1
