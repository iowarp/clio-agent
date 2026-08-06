"""Content-block preservation through the MCP execution boundary (#1188 MCP half).

A kit tool can return typed MCP content blocks -- ``ImageContent``,
``AudioContent``, an ``EmbeddedResource``, or several ``TextContent`` blocks in
one result -- with NO ``structuredContent`` at all (confirmed live against
FastMCP: ``fastmcp.client.mixins.tools._parse_call_tool_result`` derives
``CallToolResult.data`` ONLY from ``structuredContent``, so a pure-content
result parses to ``data=None``). Two lanes read that result:

* the MODEL-facing lane (``tools/mcp_executor.py::_result_to_text``, this
  module's target for the placeholder tests) -- before this fix, ``data=None``
  flowed straight into ``json.dumps(None)`` and the model observed the
  literal string ``"null"`` for a tool result that plainly carried evidence;
* the WIRE-facing lane (``tools/mcp_results.py::call_tool_result_to_observer``,
  which every tool_result Part / ledger row / durable-trace payload reads) --
  before this fix, an oversized binary block (e.g. a rendered chart PNG) rode
  through unbounded, including into the JSON text preview built from the same
  dict (``gact/evidence.py::_tool_result_preview``).

These tests drive a REAL in-memory FastMCP server through the real
``SyncMCPToolExecutor`` (the CLI/DSPy execution boundary), never a
hand-rolled fake result shape.
"""

from __future__ import annotations

import base64
import struct

import mcp_types
from fastmcp import FastMCP
from fastmcp.utilities.types import Image

from clio_agent.tools.execution import SyncMCPToolExecutor
from clio_agent.tools.mcp_executor import _raster_dimensions
from clio_agent.tools.mcp_results import (
    CONTENT_BLOCK_ELISION_REASON,
    MAX_CONTENT_BLOCK_BYTES,
    call_tool_result_to_observer,
)


def _minimal_png_bytes(width: int, height: int, *, padding: int = 0) -> bytes:
    """Bytes shaped like a real PNG signature + IHDR chunk.

    Not a fully valid/decodable image (the CRC is fake and there is no
    IDAT/IEND) -- the dimension probe under test only ever reads the fixed
    8-byte signature + 16-byte IHDR payload, never validates checksums or
    later chunks, so this is a faithful, minimal fixture for it.
    """

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_payload = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_payload + b"\x00\x00\x00\x00"
    return signature + ihdr + (b"\x00" * padding)


def _image_server(data: bytes, *, fmt: str = "png") -> FastMCP:
    server = FastMCP("image-block-probe")

    @server.tool
    def render_chart() -> Image:
        """Return a rendered chart image (pure content, no structured output)."""

        return Image(data=data, format=fmt)

    return server


# --------------------------------------------------------------------------- #
# 1. An image block survives to the observer projection (the Part's          #
#    content_blocks field reads straight from this projection).              #
# --------------------------------------------------------------------------- #


def test_image_content_block_survives_to_observer_with_dimensions() -> None:
    """A small (well under the cap) image block rides through UNELIDED, with
    its real base64 data and mimeType intact -- the typed carrier the Part's
    ``content_blocks`` field (gact/tool_observer.py) reads from directly."""

    png_bytes = _minimal_png_bytes(800, 600, padding=64)
    server = _image_server(png_bytes)

    with SyncMCPToolExecutor(server, timeout=5.0) as executor:
        raw_result = executor.call_tool_result("render_chart", {})

    observed = call_tool_result_to_observer(raw_result)
    blocks = observed["content"]
    assert len(blocks) == 1
    block = blocks[0]
    assert block["type"] == "image"
    assert block["mimeType"] == "image/png"
    assert "elided" not in block
    assert block["data"] == base64.b64encode(png_bytes).decode()


# --------------------------------------------------------------------------- #
# 2. Oversize image: elided with a typed marker, never raw/partial base64.    #
# --------------------------------------------------------------------------- #


def test_oversized_image_is_elided_with_typed_marker() -> None:
    """A block over MAX_CONTENT_BLOCK_BYTES is replaced by a typed elision
    marker (mirrors the artifact-content elision idiom) -- ``data`` dropped,
    ``elided``/``bytes`` declared, ``type``/``mimeType`` kept."""

    oversized_padding = MAX_CONTENT_BLOCK_BYTES + 4096
    png_bytes = _minimal_png_bytes(1024, 768, padding=oversized_padding)
    server = _image_server(png_bytes)

    with SyncMCPToolExecutor(server, timeout=5.0) as executor:
        raw_result = executor.call_tool_result("render_chart", {})

    observed = call_tool_result_to_observer(raw_result)
    block = observed["content"][0]
    assert block["type"] == "image"
    assert block["mimeType"] == "image/png"
    assert "data" not in block
    assert block["elided"] == CONTENT_BLOCK_ELISION_REASON
    assert block["bytes"] > MAX_CONTENT_BLOCK_BYTES
    # Sabotage twin (paired with test_image_content_block_survives_to_observer_
    # with_dimensions above): a block well UNDER the cap is never elided --
    # this is not a blanket "always elide images" behavior.


# --------------------------------------------------------------------------- #
# 3. Model text carries a placeholder, never base64 -- the SAME oversize      #
#    result that got elided on the wire lane must ALSO never leak base64      #
#    into the model-facing lane (the two lanes are independent; both must     #
#    hold).                                                                    #
# --------------------------------------------------------------------------- #


def test_model_text_is_a_dimensioned_placeholder_not_base64() -> None:
    png_bytes = _minimal_png_bytes(800, 600, padding=64)
    b64 = base64.b64encode(png_bytes).decode()
    server = _image_server(png_bytes)

    with SyncMCPToolExecutor(server, timeout=5.0) as executor:
        text = executor.call_tool("render_chart", {})

    assert text == "[image image/png 800x600]"
    assert b64 not in text
    assert text != "null"


def test_model_text_for_oversized_image_is_still_a_placeholder_not_base64() -> None:
    """Even an OVERSIZED image (elided on the wire lane) must never leak its
    base64 into the model-facing lane -- the two lanes are fixed
    independently and this proves neither regresses the other."""

    oversized_padding = MAX_CONTENT_BLOCK_BYTES + 4096
    png_bytes = _minimal_png_bytes(1024, 768, padding=oversized_padding)
    b64 = base64.b64encode(png_bytes).decode()
    server = _image_server(png_bytes)

    with SyncMCPToolExecutor(server, timeout=5.0) as executor:
        text = executor.call_tool("render_chart", {})

    assert text == "[image image/png 1024x768]"
    assert b64 not in text
    assert b64[:200] not in text


def test_model_text_falls_back_to_byte_size_for_undecodable_dimensions() -> None:
    """A non-PNG/GIF mime (or a garbled prefix) still gets a compact
    placeholder -- a byte-size detail, never a raised exception or "null"."""

    from clio_agent.tools.mcp_executor import _result_to_text

    data = base64.b64encode(b"not a real jpeg" * 50).decode()
    block = mcp_types.ImageContent(type="image", data=data, mime_type="image/jpeg")
    result = type("Result", (), {"data": None, "content": [block]})()

    text = _result_to_text(result)
    assert text.startswith("[image image/jpeg ")
    assert text.endswith("]")
    assert data not in text


# --------------------------------------------------------------------------- #
# 4. Multiple text blocks + an EmbeddedResource all survive as distinct       #
#    typed blocks (not flattened into one JSON blob) -- the fuller "explore   #
#    and close" ask beyond the image case.                                    #
# --------------------------------------------------------------------------- #


def test_multiple_text_blocks_and_embedded_resource_all_survive_distinctly() -> None:
    server = FastMCP("multi-block-probe")

    # Deliberately UNANNOTATED return: a ``-> list[Any]`` hint makes FastMCP
    # derive a JSON output schema and additionally wrap the return as
    # ``structuredContent`` (each block re-dumped as a plain dict, losing its
    # ContentBlock identity) -- realistic content-block-only tools (e.g. one
    # returning ``fastmcp.Image``) carry no such schema, so this fixture must
    # not accidentally acquire one either.
    @server.tool
    def multi_block():
        """Return several distinct content blocks with no structured output."""

        return [
            mcp_types.TextContent(type="text", text="first observation"),
            mcp_types.TextContent(type="text", text="second observation"),
            mcp_types.EmbeddedResource(
                type="resource",
                resource=mcp_types.TextResourceContents(
                    uri="file:///notes.txt",
                    mime_type="text/plain",
                    text="note body",
                ),
            ),
        ]

    with SyncMCPToolExecutor(server, timeout=5.0) as executor:
        raw_result = executor.call_tool_result("multi_block", {})
        text = executor.call_tool("multi_block", {})

    observed = call_tool_result_to_observer(raw_result)
    blocks = observed["content"]
    assert [b["type"] for b in blocks] == ["text", "text", "resource"]
    assert blocks[0]["text"] == "first observation"
    assert blocks[1]["text"] == "second observation"
    assert blocks[2]["resource"]["text"] == "note body"
    assert blocks[2]["resource"]["uri"] == "file:///notes.txt"

    # The model-facing lane joins text blocks verbatim and adds a compact
    # placeholder for the resource -- never a JSON dump of the whole result.
    assert text == (
        "first observation\nsecond observation\n[resource text/plain file:///notes.txt]"
    )


# --------------------------------------------------------------------------- #
# 5. Dimension probe unit coverage (PNG/GIF happy path, unknown format,       #
#    garbled prefix) -- isolates the helper from the full MCP round trip.     #
# --------------------------------------------------------------------------- #


def test_raster_dimensions_reads_png_ihdr() -> None:
    png_bytes = _minimal_png_bytes(1920, 1080, padding=200)
    data = base64.b64encode(png_bytes).decode()
    assert _raster_dimensions("image/png", data) == (1920, 1080)


def test_raster_dimensions_reads_gif_logical_screen_descriptor() -> None:
    gif_bytes = b"GIF89a" + struct.pack("<HH", 320, 240) + b"\x00" * 20
    data = base64.b64encode(gif_bytes).decode()
    assert _raster_dimensions("image/gif", data) == (320, 240)


def test_raster_dimensions_returns_none_for_unknown_mime() -> None:
    png_bytes = _minimal_png_bytes(100, 100)
    data = base64.b64encode(png_bytes).decode()
    assert _raster_dimensions("image/jpeg", data) is None


def test_raster_dimensions_returns_none_for_garbled_prefix() -> None:
    assert _raster_dimensions("image/png", "not-valid-base64!!!") is None
    assert _raster_dimensions("image/png", "") is None
