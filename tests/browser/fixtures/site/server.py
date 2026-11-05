"""The browser fixture site: one standard-library HTTP server for two hosts.

``python tests/browser/fixtures/site/server.py --port 8790 --state DIR`` serves
``http://a.fixture.localhost:8790`` and ``http://b.fixture.localhost:8790``; Chromium
resolves ``*.localhost`` to loopback, so the two names are two sites with no DNS setup.
Pages (on either host):

- ``/`` index; ``/signin`` (a form with a password field; a POST sets a persistent
  cookie) and ``/account`` (needs it);
- ``/table?page=N`` a paginated table of 23 rows, 10 per page, with a Next link;
- ``/form`` text, select, three checkboxes and a file input; a POST to ``/form`` is
  appended to ``DIR/submissions.jsonl`` and echoed back;
- ``/link-to-b`` a link to the other host, ``/redirect-to-b`` a 302 to it, and
  ``/redirect-link`` a link on this host that leads through that redirect;
- ``/frames`` an iframe ``#results`` holding a button; ``/download`` a file link;
- ``/slow?ms=N`` answers after N milliseconds; ``/list`` renders its list from script;
- ``/timed-redirect`` moves to the other host after five seconds (W20);
- ``/settings`` a form whose Save button disables after the email changes;
- ``/status`` a device status page (a printer's nozzle temperature).

``--tls-port 8791`` also serves the same pages over ``https://`` as ``c.fixture.localhost``
(browser addendum B, W21), with a self-signed certificate made by ``openssl`` into
``DIR/tls``. ``POST /tls/regenerate`` on the plain server makes a new key and certificate;
connections after it get the new one (W22).

Standard library and the ``openssl`` command only, so it runs with any Python and in tests.
"""

from __future__ import annotations

import argparse
import email.parser
import email.policy
import html
import json
import re
import ssl
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

#: The rows of ``/table``: (name, format, year). Deterministic, so tests can compare.
ROWS: tuple[tuple[str, str, int], ...] = tuple(
    (f"Dataset {i:02d}", ("HDF5", "NetCDF", "Zarr", "ADIOS2")[i % 4], 2020 + i % 7)
    for i in range(1, 24)
)
PAGE_ROWS = 10
COOKIE = "fixture_session"
#: The account the sign-in form accepts.
USER, PASSWORD = "ada", "correct horse"


def _page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title></head><body><h1>{html.escape(title)}</h1>"
        f"{body}</body></html>"
    ).encode()


def _other(host: str) -> str:
    name, _, port = host.partition(":")
    other = "b.fixture.localhost" if name.startswith("a.") else "a.fixture.localhost"
    return f"{other}:{port}" if port else other


class Handler(BaseHTTPRequestHandler):
    """One request; the routes are in the module docstring."""

    server: "FixtureServer"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib name
        self.server.log.append(format % args)

    # ---- helpers -----------------------------------------------------------------------
    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str = "text/html; charset=utf-8",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _host(self) -> str:
        return self.headers.get("Host") or "a.fixture.localhost"

    def _signed_in(self) -> bool:
        return f"{COOKIE}=ok" in (self.headers.get("Cookie") or "")

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    # ---- GET ---------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        name = re.sub(r"[^a-z0-9]", "_", url.path.strip("/").lower()) or "index"
        route = getattr(self, "get_" + name, None)
        if route is None:
            self._send(404, _page("Not found", f"<p>No page at {html.escape(url.path)}.</p>"))
            return
        route(query)

    def get_status(self, _query: dict[str, list[str]]) -> None:
        self._send(
            200,
            _page("Printer status", "<p>Nozzle temperature: <b id='nozzle'>215 °C</b></p>"),
        )

    def get_index(self, _query: dict[str, list[str]]) -> None:
        links = [
            "signin",
            "account",
            "table",
            "form",
            "link-to-b",
            "redirect-to-b",
            "redirect-link",
            "frames",
            "download",
            "slow",
            "list",
            "timed-redirect",
            "settings",
        ]
        items = "".join(f"<li><a href='/{name}'>{name}</a></li>" for name in links)
        self._send(200, _page(f"Fixture site {self._host()}", f"<ul>{items}</ul>"))

    def get_signin(self, _query: dict[str, list[str]]) -> None:
        self._send(
            200,
            _page(
                "Sign in",
                "<form method='post' action='/signin'>"
                "<label>Username <input name='username' autocomplete='username'></label>"
                "<label>Password <input type='password' name='password' "
                "autocomplete='current-password'></label>"
                "<button type='submit'>Sign in</button></form>",
            ),
        )

    def get_account(self, _query: dict[str, list[str]]) -> None:
        if not self._signed_in():
            self._send(302, b"", headers={"Location": "/signin"})
            return
        self._send(200, _page("Account", f"<p id='who'>Signed in as {USER}.</p>"))

    def get_table(self, query: dict[str, list[str]]) -> None:
        page = max(1, int((query.get("page") or ["1"])[0] or 1))
        last = (len(ROWS) + PAGE_ROWS - 1) // PAGE_ROWS
        start = (page - 1) * PAGE_ROWS
        rows = "".join(
            f"<tr><td>{n}</td><td>{f}</td><td>{y}</td></tr>"
            for n, f, y in ROWS[start : start + PAGE_ROWS]
        )
        nav = f"<p>Page {page} of {last}</p>"
        if page < last:
            nav += f"<a href='/table?page={page + 1}'>Next</a>"
        self._send(
            200,
            _page(
                "Datasets",
                "<table><thead><tr><th>Name</th><th>Format</th><th>Year</th></tr></thead>"
                f"<tbody>{rows}</tbody></table>{nav}",
            ),
        )

    def get_form(self, _query: dict[str, list[str]]) -> None:
        boxes = "".join(
            f"<label><input type='checkbox' name='tags' value='{t}'> {t}</label>"
            for t in ("simulation", "observation", "archived")
        )
        self._send(
            200,
            _page(
                "Submit a dataset",
                "<form method='post' action='/form' enctype='multipart/form-data'>"
                "<label>Title <input name='title'></label>"
                "<label>Format <select name='format'><option>HDF5</option>"
                "<option>NetCDF</option><option>Zarr</option></select></label>"
                f"<fieldset><legend>Tags</legend>{boxes}</fieldset>"
                "<label>Attachment <input type='file' name='attachment'></label>"
                "<button type='submit'>Submit</button></form>",
            ),
        )

    def get_link_to_b(self, _query: dict[str, list[str]]) -> None:
        target = f"http://{_other(self._host())}/"
        self._send(200, _page("Link", f"<a id='go' href='{target}'>Go to the other site</a>"))

    def get_redirect_link(self, _query: dict[str, list[str]]) -> None:
        self._send(
            200,
            _page("Redirect link", "<a id='go' href='/redirect-to-b'>Continue to the results</a>"),
        )

    def get_redirect_to_b(self, _query: dict[str, list[str]]) -> None:
        self._send(302, b"", headers={"Location": f"http://{_other(self._host())}/"})

    def get_frames(self, _query: dict[str, list[str]]) -> None:
        self._send(200, _page("Frames", "<iframe id='results' src='/frame-inner'></iframe>"))

    def get_frame_inner(self, _query: dict[str, list[str]]) -> None:
        self._send(
            200,
            _page(
                "Inner",
                "<button onclick=\"this.textContent='Clicked'\">Inside the frame</button>",
            ),
        )

    def get_download(self, _query: dict[str, list[str]]) -> None:
        self._send(200, _page("Download", "<a href='/files/report.csv'>Download the report</a>"))

    def get_files_report_csv(self, _query: dict[str, list[str]]) -> None:
        body = "name,format,year\n" + "".join(f"{n},{f},{y}\n" for n, f, y in ROWS)
        self._send(
            200,
            body.encode(),
            "text/csv",
            {"Content-Disposition": "attachment; filename=report.csv"},
        )

    def get_readings(self, _query: dict[str, list[str]]) -> None:
        link = "<a href='/files/readings.csv'>Download the latest readings</a>"
        self._send(200, _page("Readings", link))

    def get_files_readings_csv(self, _query: dict[str, list[str]]) -> None:
        """New content under one file name on every request: a second download is a
        second version of the same artifact, not a duplicate."""
        body = f"taken_at,value\n{time.time():.6f},{time.monotonic_ns() % 1000}\n"
        self._send(
            200,
            body.encode(),
            "text/csv",
            {"Content-Disposition": "attachment; filename=readings.csv"},
        )

    def get_slow(self, query: dict[str, list[str]]) -> None:
        delay = int((query.get("ms") or ["3000"])[0] or 0)
        time.sleep(min(delay, 60_000) / 1000)
        self._send(200, _page("Slow", f"<p>Answered after {delay} ms.</p>"))

    def get_list(self, _query: dict[str, list[str]]) -> None:
        script = (
            "<ul id='items'></ul><script>setTimeout(()=>{const ul=document.getElementById"
            "('items');for(const t of ['alpha','beta','gamma']){const li=document."
            "createElement('li');li.textContent=t;ul.appendChild(li);}},500);</script>"
        )
        self._send(200, _page("Rendered list", script))

    def get_timed_redirect(self, _query: dict[str, list[str]]) -> None:
        target = f"http://{_other(self._host())}/"
        script = f"<script>setTimeout(()=>{{location.href={json.dumps(target)}}},5000);</script>"
        self._send(200, _page("Timed redirect", f"<p>Moving in five seconds.</p>{script}"))

    def get_settings(self, _query: dict[str, list[str]]) -> None:
        script = (
            "<script>document.getElementById('email').addEventListener('input',()=>"
            "{document.getElementById('save').disabled=true;});</script>"
        )
        self._send(
            200,
            _page(
                "Settings",
                "<form onsubmit='return false'><label>Email <input id='email' name='email' "
                "value='ada@example.org'></label><button id='save'>Save changes</button></form>"
                + script,
            ),
        )

    # ---- POST --------------------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        path = urlsplit(self.path).path
        if path == "/signin":
            self._signin()
        elif path == "/tls/regenerate":
            tls = getattr(self.server, "tls", None)
            if tls is None:
                self._send(404, _page("No TLS host", "<p>Started without --tls-port.</p>"))
                return
            fingerprint = tls.regenerate()
            self._send(200, json.dumps({"fingerprint": fingerprint}).encode())
        elif path == "/form":
            self._submit()
        else:
            self._send(404, _page("Not found", ""))

    def _signin(self) -> None:
        fields = parse_qs(self._body().decode())
        if (fields.get("username") or [""])[0] == USER and (fields.get("password") or [""])[
            0
        ] == PASSWORD:
            self._send(
                303,
                b"",
                headers={
                    "Location": "/account",
                    "Set-Cookie": f"{COOKIE}=ok; Path=/; Max-Age=31536000; HttpOnly",
                },
            )
            return
        self._send(401, _page("Sign in failed", "<p>Wrong username or password.</p>"))

    def _submit(self) -> None:
        raw = f"Content-Type: {self.headers.get('Content-Type')}\r\n\r\n".encode() + self._body()
        message = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw)
        record: dict[str, Any] = {"host": self._host(), "tags": []}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename is not None:
                record[str(name)] = {"filename": filename, "size": len(payload)}
            elif name == "tags":
                record["tags"].append(payload.decode())
            else:
                record[str(name)] = payload.decode()
        self.server.record(record)
        self._send(
            200, _page("Submitted", f"<pre id='echo'>{html.escape(json.dumps(record))}</pre>")
        )


class FixtureServer(ThreadingHTTPServer):
    """The server, with the submissions file and an in-memory request log."""

    daemon_threads = True

    def __init__(self, port: int, state: Path) -> None:
        super().__init__(("127.0.0.1", port), Handler)
        self.state = state
        self.state.mkdir(parents=True, exist_ok=True)
        self.log: list[str] = []
        self._lock = threading.Lock()

    def record(self, submission: dict[str, Any]) -> None:
        with self._lock, (self.state / "submissions.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(submission) + "\n")


TLS_HOST = "c.fixture.localhost"


class TlsFixture:
    """``c.fixture.localhost`` over ``https://`` with a self-signed certificate."""

    def __init__(self, port: int, state: Path) -> None:
        self.folder = state / "tls"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.context = self._make()
        self.server = FixtureServer(port, state)
        self.server.get_request = self._wrapped  # type: ignore[method-assign]

    def _make(self) -> ssl.SSLContext:
        key, cert = self.folder / "key.pem", self.folder / "cert.pem"
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:prime256v1",
                "-nodes",
                "-days",
                "30",
                "-subj",
                f"/CN={TLS_HOST}/O=Fixture Printer",
                "-addext",
                f"subjectAltName=DNS:{TLS_HOST}",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            ],
            check=True,
            capture_output=True,
        )
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        return context

    def _wrapped(self) -> tuple[Any, Any]:
        sock, address = self.server.socket.accept()
        return self.context.wrap_socket(sock, server_side=True), address

    def regenerate(self) -> str:
        self.context = self._make()
        return fingerprint(self.folder / "cert.pem")


def fingerprint(cert: Path) -> str:
    """The certificate's base64 SHA-256 SPKI fingerprint, as Chromium's flag takes it."""
    spki = subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-pubkey", "-noout"],
        check=True,
        capture_output=True,
    ).stdout
    der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-outform", "der"],
        input=spki,
        check=True,
        capture_output=True,
    ).stdout
    import base64  # noqa: PLC0415
    import hashlib  # noqa: PLC0415

    return base64.b64encode(hashlib.sha256(der).digest()).decode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--tls-port", type=int, default=0)
    parser.add_argument("--state", type=Path, default=Path(".cloud-run/fixture-site"))
    options = parser.parse_args()
    server = FixtureServer(options.port, options.state)
    if options.tls_port:
        tls = TlsFixture(options.tls_port, options.state)
        server.tls = tls  # type: ignore[attr-defined]
        threading.Thread(target=tls.server.serve_forever, daemon=True).start()
        print(f"fixture site on https://{TLS_HOST}:{tls.server.server_port}", flush=True)
    print(f"fixture site on http://a.fixture.localhost:{server.server_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
