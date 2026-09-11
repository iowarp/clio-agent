from __future__ import annotations

import pytest

from clio_agent.gact.ask_user_tool import AskUserError, _validated_question


def test_ask_user_rejects_internal_parser_markers() -> None:
    with pytest.raises(AskUserError, match="internal parser marker"):
        _validated_question("Finalize[[ ## next_thought ##")


def test_ask_user_preserves_clean_user_facing_question() -> None:
    assert (
        _validated_question("Finalize the comparison after the parent constraint arrives?")
        == "Finalize the comparison after the parent constraint arrives?"
    )
