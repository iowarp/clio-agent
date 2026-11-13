"""A view_image/view_pdf call puts the media in front of the model, direct or from a script.

The reported failure: an agent called ``view_image`` only from ``run_code`` scripts
(several at once via ``asyncio.gather``), got back the metadata dict, and never saw a
pixel. These tests pin where each view reaches the model, on every provider wire, and
that a view which cannot reach it says so instead of reading as success.
"""

from __future__ import annotations

import base64
import contextvars
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import dspy
import pytest
from dspy.lm15 import ImagePart, Message, Request, TextPart, ToolCallPart, ToolResultPart

from clio_agent.gact.agents.clio_react import ClioReAct, _place_tool_media
from clio_agent.gact.agents.clio_react_record import result_part
from clio_agent.gact.agents.native_presenters_workspace_file import (
    SEEN_BY_MODEL,
    workspace_file_presentation,
)
from clio_agent.gact.agents.tool_settings import apply_tool_settings
from clio_agent.gact.code_mode import kernels
from clio_agent.gact.code_mode import tool as code_tool
from clio_agent.gact.code_mode.presentation import present
from clio_agent.gact.model_media import (
    CALL_MEDIA,
    DETACHED,
    SCRIPT_MEDIA,
    MediaNotShownError,
    deliver,
)
from clio_agent.gact.tool_call_nesting import PARENT_CALL_ID
from clio_agent.gact.view_image_tool import ViewImageError, build_view_image_tool
from clio_agent.tools.execution import tool_workspace_context
from tests._scripted_engine import Reply, calls, scripted_lm

pytestmark = pytest.mark.usefixtures("clio_core_plane")

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6ZQAAAABJRU5ErkJggg=="
)
_DESCRIPTOR = {
    "type": "clio.workspace_image.v1",
    "path": "page-1.png",
    "media_type": "image/png",
    "sha256": "a" * 64,
}


@pytest.fixture
def pages(tmp_path: Path) -> Iterator[Path]:
    for i in range(1, 5):
        (tmp_path / f"page-{i}.png").write_bytes(_PNG)
    with tool_workspace_context(tmp_path):
        yield tmp_path


def _in_slot(slot: Any, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Run ``fn`` the way a script's nested call runs: a copied context holding ``slot``."""
    token = SCRIPT_MEDIA.set(slot)
    try:
        context = contextvars.copy_context()
    finally:
        SCRIPT_MEDIA.reset(token)
    return context.run(fn, *args, **kwargs)


# -- where a view lands -------------------------------------------------------------


def test_outside_a_model_step_the_descriptor_is_unchanged() -> None:
    assert deliver(dict(_DESCRIPTOR), tool="view_image") == _DESCRIPTOR


def test_a_direct_call_is_attached_as_its_own_result() -> None:
    token = CALL_MEDIA.set([])
    try:
        out = deliver(dict(_DESCRIPTOR), tool="view_image")
    finally:
        CALL_MEDIA.reset(token)
    assert out["attached_to_model"] is True and out["delivery"] == "tool_result"


def test_a_script_view_joins_the_calling_run_codes_result() -> None:
    slot: list[dict[str, Any]] = []
    token = PARENT_CALL_ID.set("call_7")
    try:
        out = _in_slot(slot, deliver, dict(_DESCRIPTOR), tool="view_image")
    finally:
        PARENT_CALL_ID.reset(token)
    assert slot == [_DESCRIPTOR]
    assert (out["attached_to_model"], out["delivery"], out["parent_call_id"]) == (
        True,
        "script_result",
        "call_7",
    )
    assert "next step" in out["note"]


def test_a_background_script_view_is_refused_in_words(pages: Path) -> None:
    view = build_view_image_tool()
    with pytest.raises(ViewImageError) as raised:
        _in_slot(DETACHED, view, path="page-1.png")
    assert raised.value.reason == "view_image_not_shown"
    assert "NOT shown to the model" in str(raised.value)


def test_a_script_past_its_media_limit_is_refused_in_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLIO_TOOLS_SCRIPT_MEDIA_MAX", "1")
    slot: list[dict[str, Any]] = []
    _in_slot(slot, deliver, dict(_DESCRIPTOR), tool="view_image")
    with pytest.raises(MediaNotShownError) as raised:
        _in_slot(slot, deliver, {**_DESCRIPTOR, "path": "page-2.png"}, tool="view_image")
    assert raised.value.reason == "view_image_script_limit"
    assert "page-2.png was NOT shown" in str(raised.value)
    assert len(slot) == 1


# -- the provider-wire part ----------------------------------------------------------


def test_script_media_follows_the_script_text_as_real_pixels(pages: Path) -> None:
    descriptor = build_view_image_tool()(path="page-2.png")
    part = result_part("c1", "run_code", "rendered 4 pages", False, media=[descriptor])

    text, label, image = part.content
    assert isinstance(text, TextPart) and text.text == "rendered 4 pages"
    assert isinstance(label, TextPart) and "page-2.png" in label.text
    assert isinstance(image, ImagePart) and base64.b64decode(image.data) == _PNG


def test_script_media_the_history_lost_is_a_note_not_a_failure(pages: Path) -> None:
    descriptor = build_view_image_tool()(path="page-1.png")
    (pages / descriptor["snapshot"]).unlink()
    part = result_part("c1", "run_code", "ok", False, media=[descriptor])

    _text, _label, note = part.content
    assert isinstance(note, TextPart)
    assert note.text.startswith("[clio: media_unavailable]")


def _wire_body(lm: Any, request: Request) -> str:
    built = lm.build_request(request, False)
    body = getattr(built, "json", None) or built.body
    return body.decode() if isinstance(body, bytes) else json.dumps(body)


@pytest.mark.parametrize(
    ("provider", "placement", "model", "marker"),
    [
        ("anthropic.AnthropicLM", "native", "claude-sonnet-4-5", '"type": "image"'),
        ("openai.OpenAILM", "native", "gpt-5", '"type": "input_image"'),
        ("openai_codex.OpenAICodexLM", "native", "gpt-5-codex", '"type": "input_image"'),
        ("gemini.GeminiLM", "user_message", "gemini-2.5-pro", '"inlineData"'),
        ("openai_chat.OpenAIChatLM", "user_message", "gpt-4o", '"image_url"'),
    ],
)
def test_every_provider_wire_carries_a_script_viewed_image(
    provider: str, placement: str, model: str, marker: str
) -> None:
    import importlib

    module, cls = provider.rsplit(".", 1)
    lm_cls = getattr(importlib.import_module(f"dspy._vendor.lm15.providers.{module}"), cls)
    extra = {"account_id": "acct-test"} if "codex" in module else {}
    lm = lm_cls(api_key="test", **extra)
    image = ImagePart(data=base64.b64encode(_PNG).decode(), media_type="image/png")
    result = ToolResultPart(
        id="c1",
        name="run_code",
        content=(TextPart(text="rendered"), TextPart(text="[image viewed: p.png]"), image),
    )
    messages = [
        Message.user("look"),
        Message(role="assistant", parts=(ToolCallPart(id="c1", name="run_code", input={}),)),
        Message(role="tool", parts=(result,)),
    ]
    request = Request(model=model, messages=tuple(_place_tool_media(messages, placement)))

    body = json.dumps(json.loads(_wire_body(lm, request)), separators=(", ", ": "))
    assert marker in body
    assert image.data in body


def test_claude_code_and_copilot_text_wires_keep_the_image() -> None:
    from clio_agent.lm.engines.text_tools import render_messages

    image = ImagePart(data=base64.b64encode(_PNG).decode(), media_type="image/png")
    result = ToolResultPart(
        id="c1", name="run_code", content=(TextPart(text="ok"), TextPart(text="[p.png]"), image)
    )
    text, media = render_messages([Message(role="tool", parts=(result,))])

    assert media == [image]
    assert "[p.png]\n(image 1 attached)" in text


# -- through the loop -----------------------------------------------------------------


def _script_tool() -> dspy.Tool:
    """A stand-in for run_code: its "script" views two pages the way a kernel call does."""
    view = build_view_image_tool()

    def script(code: str) -> str:
        slot = CALL_MEDIA.get()
        for name in ("page-1.png", "page-3.png"):
            _in_slot(slot, view, path=name)
        return "rendered 4 pages"

    return dspy.Tool(script, name="script", desc="Run a script.")


def _tool_results(request: Request) -> list[ToolResultPart]:
    return [
        part
        for message in request.messages
        for part in message.parts
        if isinstance(part, ToolResultPart)
    ]


def test_the_loop_shows_a_scripts_views_on_every_later_step(pages: Path) -> None:
    lm, engine = scripted_lm(
        [
            calls(("script", {"code": "render()"})),
            calls(("noop", {})),
            Reply(text="Page 1 is a bar chart."),
        ]
    )
    noop = dspy.Tool(lambda: "done", name="noop", desc="Nothing.")
    with dspy.context(lm=lm):
        ClioReAct(cast(Any, "question -> answer"), tools=[_script_tool(), noop])(
            question="Check the pages."
        )

    for request in engine.requests[1:3]:  # the next step, and the history after it
        [script, *_] = _tool_results(request)
        images = [p for p in script.content if isinstance(p, ImagePart)]
        labels = [p.text for p in script.content if isinstance(p, TextPart)]
        assert [base64.b64decode(i.data) for i in images] == [_PNG, _PNG]
        assert any("page-3.png" in text for text in labels)


def test_a_direct_view_in_the_loop_records_that_it_was_attached(pages: Path) -> None:
    lm, engine = scripted_lm([calls(("view_image", {"path": "page-1.png"})), Reply(text="ok")])
    with dspy.context(lm=lm):
        ClioReAct(cast(Any, "question -> answer"), tools=[build_view_image_tool()])(
            question="What is visible?"
        )

    [result] = _tool_results(engine.requests[1])
    assert [type(p) for p in result.content] == [ImagePart]


# -- a real kernel, gathering views ---------------------------------------------------


@pytest.mark.timeout(120)  # starts a real kernel process
def test_a_kernel_script_gathering_views_attaches_every_page(pages: Path) -> None:
    agent = SimpleNamespace(
        tools={"view_image": build_view_image_tool()},
        _clio_tool_servers=frozenset(),
        _clio_deferred_catalog=None,
    )
    apply_tool_settings(agent, SimpleNamespace(tools=[], mcp_servers={}))
    code = (
        "import asyncio\n"
        "rows = await asyncio.gather(*(tools.view_image(path=f'page-{i}.png') "
        "for i in range(1, 5)))\n"
        "print([r['delivery'] for r in rows])"
    )
    slot: list[dict[str, Any]] = []
    token = CALL_MEDIA.set(slot)
    try:
        outcome = code_tool.run_script(agent, code, session_id="mm", scope="main")
    finally:
        CALL_MEDIA.reset(token)
        kernels.close_session("mm")

    assert outcome["status"] == "completed", outcome
    assert outcome["output"].strip() == str(["script_result"] * 4)
    assert sorted(item["path"] for item in slot) == [f"page-{i}.png" for i in range(1, 5)]


# -- what the transcript shows ---------------------------------------------------------


def test_run_code_says_and_shows_what_its_script_attached() -> None:
    outcome = {
        "status": "completed",
        "output": "done",
        "calls": 2,
        "failed_calls": 0,
        "attached_media": [
            {"path": "page-1.png", "media_type": "image/png", "sha256": "a" * 64},
            {"path": "page-2.png", "media_type": "image/png", "sha256": "b" * 64},
        ],
    }
    assert "Attached below: the 2 images/PDFs this script viewed" in code_tool.model_text(outcome)

    view = present({"code": "x"}, "done", outcome)
    attached = [b for b in view["blocks"] if str(b.get("id", "")).startswith("attached-")]
    assert [(b["path"], b["status"]) for b in attached] == [
        ("page-1.png", SEEN_BY_MODEL),
        ("page-2.png", SEEN_BY_MODEL),
    ]
    assert all("run_code script's result" in b["detail"] for b in attached)


def test_a_view_image_row_says_it_was_shown_to_the_model() -> None:
    seen = {**_DESCRIPTOR, "attached_to_model": True, "delivery": "tool_result"}

    view = workspace_file_presentation({"path": "page-1.png"}, seen, None)
    [block] = view["blocks"]
    assert view["summary"] == "Shown to the model"
    assert (block["status"], block["detail"]) == (
        SEEN_BY_MODEL,
        "Shown to the model on its next step, as this call's result.",
    )

    unseen = workspace_file_presentation({"path": "page-1.png"}, dict(_DESCRIPTOR), None)
    assert unseen["summary"] == "" and "status" not in unseen["blocks"][0]
