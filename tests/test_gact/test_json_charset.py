"""JSON responses name their charset, so a client that defaults to ISO-8859-1 (Windows
PowerShell 5.1's ``Invoke-RestMethod``) reads "8°" and "289 × 135" as sent (W17 take 1o)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.testclient import TestClient

from clio_agent.gact.protocol.json_charset import install_json_charset, with_utf8_charset

ASK = "NACA 0012 at 8° incidence, 289 × 135 × 8 grid"


def _client() -> TestClient:
    app = FastAPI()

    @app.get("/ask")
    def ask() -> dict[str, str]:
        return {"message": ASK}

    @app.get("/problem")
    def problem() -> JSONResponse:
        return JSONResponse({"detail": ASK}, media_type="application/problem+json")

    @app.get("/text")
    def text() -> PlainTextResponse:
        return PlainTextResponse(ASK)

    @app.get("/stream")
    def stream() -> StreamingResponse:
        return StreamingResponse(iter([b"data: 1\n\n"]), media_type="text/event-stream")

    install_json_charset(app)
    return TestClient(app)


def test_a_json_response_names_utf8_so_its_text_survives_a_header_driven_decode() -> None:
    """SABOTAGE: drop ``install_json_charset(app)`` and the header has no charset -> red."""
    response = _client().get("/ask")
    content_type = response.headers["content-type"]
    assert content_type == "application/json; charset=utf-8"
    charset = content_type.split("charset=", 1)[1]
    assert response.content.decode(charset) == '{"message":"' + ASK + '"}'
    assert "Â" not in response.content.decode(charset)


def test_json_suffix_types_get_it_and_other_types_keep_theirs() -> None:
    client = _client()
    assert client.get("/problem").headers["content-type"] == (
        "application/problem+json; charset=utf-8"
    )
    assert client.get("/text").headers["content-type"] == "text/plain; charset=utf-8"
    assert client.get("/stream").headers["content-type"].startswith("text/event-stream")


def test_a_named_charset_is_kept() -> None:
    assert with_utf8_charset(b"application/json; charset=utf-16") == (
        b"application/json; charset=utf-16"
    )
    assert with_utf8_charset(b"image/png") == b"image/png"
