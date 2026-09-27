# GACT Browser-Origin Security

CLIO's GACT backend uses `trust_socket` for local clients: a request from a
loopback peer needs no bearer token, and a server with no token configured
needs none from anyone. That is appropriate for native local clients, but
browsers are different: a page loaded from an unrelated site can make the
user's browser send requests to `http://127.0.0.1:<port>`.

CLIO applies three layers to requests that carry no valid bearer token
(`src/clio_agent/gact/origin_guard.py`). A request with a valid token is not
inspected.

## 1. CORS: which pages may read responses

By default, CLIO grants CORS to no browser origin outside the local dev ports.
Browser or WebView clients on another origin must opt in explicitly:

```bash
export CLIO_GACT_CORS_ORIGINS=http://localhost:4173,tauri://localhost
```

`CLIO_GACT_CORS_ORIGINS=*` remains available for controlled development
environments, but it should not be used with an untrusted browser and a
localhost agent that accepts `trust_socket`.

## 2. Origin: which pages may change state

CORS only stops a page from reading a response; a cross-origin "simple"
request (a form-style POST) still reaches the route. A token-less `POST`,
`PUT`, `PATCH` or `DELETE` is therefore refused with `403 origin_not_allowed`
when its `Origin` is not allowed, or when it is marked
`Sec-Fetch-Site: cross-site` without an allowed `Origin`. Allowed origins:

- the Desktop WebView (`tauri://localhost`, `http://tauri.localhost`,
  `https://tauri.localhost`);
- `gact.cors.origins`;
- the server's own origin for the same-origin web UI (`Origin` equal to
  `scheme://Host`, on an allowed host).

Requests without an `Origin` header, such as the native TUI, the desktop's
native HTTP bridge, curl and local scripts, continue to work.

## 3. Host: DNS rebinding

A foreign domain re-pointed at 127.0.0.1 makes its page same-origin with CLIO,
so CORS does not apply. The browser still sends the foreign name in `Host`.
Every token-less request (any method, and WebSocket upgrades) must therefore
address `localhost`, `127.0.0.1` or `[::1]` (any port), or a host name listed
in `gact.allowed_hosts`; anything else gets `403 host_not_allowed`.

A LAN or container deployment reached by another name lists it explicitly:

```bash
export CLIO_GACT_ALLOWED_HOSTS=clio.lab.example,192.168.1.20
```

`*` allows any host name, which gives up the DNS-rebinding protection; prefer
a bearer token (`gact.auth.bearer_token`) for anything reachable beyond this
machine.
