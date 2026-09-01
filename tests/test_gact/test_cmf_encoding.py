"""Backslashed property values survive the cmf-server, losslessly.

The upstream defect these pin: an execution whose properties or custom
properties contain a literal backslash is discarded WHOLE (with its events) by
the cmf-server, which still answers 200 {"status": "success"}. Characterised
live -- the backslash is the only trigger; newline, tab, quote, unicode, percent
and 400-char values all survive.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from clio_agent.gact.artifacts.provenance.cmf_document import (
    artifact_entry,
    build_push_document,
    execution_entry,
)
from clio_agent.gact.artifacts.provenance.cmf_encoding import (
    decode_properties,
    decode_property_value,
    encode_properties,
    encode_property_value,
    has_hostile_value,
    posix_path,
)

# The real killer from the live run: tool arguments carrying Windows paths.
_WINDOWS_INSTRUMENT = json.dumps(
    {
        "tool": "fs_apply_edit_write",
        "args": {"path": "D:\\Libraries\\Documents\\projects\\clio-agent\\a3.csv"},
        "cwd": "D:\\Libraries\\Documents",
    }
)


@pytest.mark.parametrize(
    "value",
    [
        "D:\\Lib\\a.csv",
        "a\\b",
        "abc\\",
        "\\",
        "\\\\server\\share\\file",
        _WINDOWS_INSTRUMENT,
        # Percent must survive too, or the encoding is not a bijection.
        "100%",
        "%5C",
        "%25",
        "a%b\\c",
        # Everything the live probe proved harmless must round-trip unchanged.
        "plain",
        "a\nb",
        "a\tb",
        'a"b',
        "a\u00e9b",
        "x" * 400,
    ],
)
def test_encoding_is_a_lossless_bijection(value: str) -> None:
    encoded = encode_property_value(value)
    assert decode_property_value(encoded) == value


@pytest.mark.parametrize("value", ["D:\\Lib\\a.csv", "a\\b", "abc\\", "\\", _WINDOWS_INSTRUMENT])
def test_encoded_values_carry_no_backslash_the_server_would_choke_on(value: str) -> None:
    assert "\\" not in str(encode_property_value(value))


def test_values_needing_nothing_are_returned_untouched() -> None:
    """The common case must not churn: no backslash, no percent, no change."""
    assert encode_property_value("fs_apply_edit_write") == "fs_apply_edit_write"
    assert encode_property_value("{}") == "{}"


def test_non_strings_stay_typed_so_mlmd_keeps_them_as_ints() -> None:
    assert encode_property_value(12) == 12
    assert encode_property_value(None) is None
    assert decode_property_value(7) == 7


def test_a_literal_escape_sequence_in_the_original_survives() -> None:
    """A value that already contains "%5C" must not decode into a backslash."""
    original = "literal %5C here"
    assert decode_property_value(encode_property_value(original)) == original


def test_posix_path_is_the_faithful_representation_of_a_windows_path() -> None:
    assert posix_path("D:\\Libraries\\a.csv") == "D:/Libraries/a.csv"
    assert posix_path("/already/posix") == "/already/posix"


def test_property_mappings_round_trip() -> None:
    properties = {"clio_path": "D:\\a\\b.csv", "clio_version": 2, "clio_kind": "dataset"}
    assert decode_properties(encode_properties(properties)) == properties


def test_hostile_detector_sees_a_raw_backslash_and_not_an_encoded_one() -> None:
    assert has_hostile_value({"k": "D:\\a"}) is True
    assert has_hostile_value(encode_properties({"k": "D:\\a"})) is False
    assert has_hostile_value({"k": 3, "j": "clean"}) is False


# --------------------------------------------------------------------------- #
# End to end: the synthesized document must never carry a raw backslash into an
# execution, because that silently discards the execution AND its events.
# --------------------------------------------------------------------------- #


def _artifact_event(artifact_id: str, name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    event = {"event_id": f"sem_{artifact_id}"}
    body = {
        "artifact_id": artifact_id,
        "name": name,
        "version": 1,
        "kind": "dataset",
        "path": "D:\\Libraries\\Documents\\a3.csv",
        "producer": {
            "call_id": "call_1",
            "tool": "fs_apply_edit_write",
            "storage_receipt": {"object_uri": "cmf+dvc://local/files/md5/ab/cd"},
        },
    }
    return event, body


def _transform_event(call_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    event = {"event_id": f"sem_{call_id}"}
    body = {
        "call_id": call_id,
        "instrument": {
            "tool": "fs_apply_edit_write",
            "args": {"path": "D:\\Libraries\\Documents\\a3.csv"},
        },
        "environment": {"tier": "container"},
        "generated": [{"artifact_id": "artifact_1", "name": "a3.csv"}],
    }
    return event, body


def _document() -> dict[str, Any]:
    a_event, a_body = _artifact_event("artifact_1", "a3.csv")
    t_event, t_body = _transform_event("call_1")
    return build_push_document(
        pipeline_name="clio-test",
        artifacts={"artifact_1": artifact_entry(a_event, a_body)},
        executions=[execution_entry(t_event, t_body)],
    )


def test_no_execution_in_a_synthesized_document_carries_a_raw_backslash() -> None:
    """The exact shape that lost 13 executions and their whole event chain."""
    document = _document()
    for pipeline in document["Pipeline"]:
        for stage in pipeline["stages"]:
            for execution in stage["executions"]:
                assert not has_hostile_value(execution["properties"])
                assert not has_hostile_value(execution["custom_properties"])


def test_the_windows_instrument_blob_survives_and_decodes_back() -> None:
    """clio_instrument_json is the field the live bisect proved fatal."""
    execution = _document()["Pipeline"][0]["stages"][0]["executions"][0]
    encoded = execution["custom_properties"]["clio_instrument_json"]
    assert "\\" not in encoded

    restored = json.loads(decode_property_value(encoded))
    assert restored["args"]["path"] == "D:\\Libraries\\Documents\\a3.csv"


def test_declared_paths_are_posix_not_percent_escaped() -> None:
    """Readability where CLIO's schema says "this is a path"."""
    artifact = _document()["Pipeline"][0]["stages"][0]["executions"][0]["events"][0]["artifact"]
    assert artifact["custom_properties"]["clio_path"] == "D:/Libraries/Documents/a3.csv"
    assert "%5C" not in artifact["custom_properties"]["clio_path"]


def test_no_entity_value_anywhere_carries_a_raw_backslash() -> None:
    """Artifacts tolerate one today, but the defect is the server's to fix, not
    a guarantee to lean on.

    Asserted on decoded VALUES, never on the serialized document: JSON's own
    ``\\"`` quote escaping puts backslashes in the wire text legitimately, and
    the server chokes on the value, not on the transport encoding.
    """
    document = _document()
    for pipeline in document["Pipeline"]:
        for stage in pipeline["stages"]:
            for execution in stage["executions"]:
                assert not has_hostile_value(execution["properties"])
                assert not has_hostile_value(execution["custom_properties"])
                for event in execution["events"]:
                    artifact = event["artifact"]
                    assert not has_hostile_value(artifact["properties"])
                    assert not has_hostile_value(artifact["custom_properties"])
                    assert "\\" not in artifact["name"]
                    assert "\\" not in artifact["uri"]
