# GACT Browser-Origin Security

CLIO's GACT backend uses `trust_socket` for local clients. That is appropriate
for native local clients, but browser origins are different: a page loaded from
an unrelated site can send requests to `http://127.0.0.1:<port>` unless the
server refuses to grant CORS access.

By default, CLIO does not allow any browser origin. Requests without an
`Origin` header, such as native TUI, desktop bridge, curl, and local scripts,
continue to work. Browser or WebView clients must opt in explicitly:

```bash
export CLIO_GACT_CORS_ORIGINS=http://localhost:4173,tauri://localhost
```

`CLIO_GACT_CORS_ORIGINS=*` remains available for controlled development
environments, but it should not be used with an untrusted browser and a
localhost agent that accepts `trust_socket`.
