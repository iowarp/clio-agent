"""A request carries at most the endpoint's per-prompt image count (the newest ones).

Live (ALCF nemotron-35-super-vl, 2026-11-25): after the model viewed a second
figure, the gateway answered HTTP 400 "At most 1 image(s) may be provided in one
prompt" and the turn ended on that error text.
"""

from __future__ import annotations

from dspy.lm15 import ImagePart, Message, TextPart, ToolResultPart

from clio_agent.gact.agents.clio_react_media import _place_tool_media, _within_image_budget


def _view(call_id: str, data: str) -> Message:
    image = ImagePart(data=data, media_type="image/png")
    result = ToolResultPart(id=call_id, name="view_image", content=(TextPart(text=call_id), image))
    return Message(role="tool", parts=(result,))


def _images(messages: list[Message]) -> list[str]:
    found: list[str] = []
    for message in messages:
        for part in message.parts:
            content = part.content if isinstance(part, ToolResultPart) else (part,)
            found += [p.data for p in content if isinstance(p, ImagePart)]
    return found


def _conversation() -> list[Message]:
    return [
        Message(role="user", parts=(TextPart(text="make the figure"),)),
        _view("c1", "Zmlyc3Q="),
        Message(role="assistant", parts=(TextPart(text="again"),)),
        _view("c2", "c2Vjb25k"),
    ]


def test_only_the_newest_images_are_sent_and_the_older_one_leaves_a_note() -> None:
    messages = _conversation()
    out = _within_image_budget(messages, 1)
    assert _images(out) == ["c2Vjb25k"]
    first = out[1].parts[0].content
    assert [p.text for p in first if isinstance(p, TextPart)][-1].startswith(
        "[earlier image omitted"
    )
    assert _images(messages) == ["Zmlyc3Q=", "c2Vjb25k"]  # the record is not touched


def test_no_cap_sends_every_image() -> None:
    messages = _conversation()
    assert _within_image_budget(messages, 0) is messages
    assert _images(_within_image_budget(messages, 2)) == ["Zmlyc3Q=", "c2Vjb25k"]


def test_the_cap_holds_after_media_moves_to_user_messages() -> None:
    placed = _place_tool_media(_within_image_budget(_conversation(), 1), "user_message")
    user_images = [
        p for m in placed if m.role == "user" for p in m.parts if isinstance(p, ImagePart)
    ]
    assert [p.data for p in user_images] == ["c2Vjb25k"]


def test_the_cap_is_read_from_configuration(monkeypatch) -> None:
    from clio_agent.lm.factory import _max_request_images

    monkeypatch.setenv("CLIO_LM_MAX_REQUEST_IMAGES", "1")
    assert _max_request_images() == 1
    monkeypatch.delenv("CLIO_LM_MAX_REQUEST_IMAGES")
    assert _max_request_images() == 0
