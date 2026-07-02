# Workspace File Preview

`GET /v1/workspaces/{wid}/files/read?path=...` returns one workspace file for
picker and preview surfaces. The endpoint returns raw `text/plain` content, not
a JSON string, so clients can render previews without stripping JSON quotes or
unescaping the body first.

The existing safety checks still apply:

- unknown workspaces return the structured GACT error envelope
- paths must stay inside the workspace root
- the file must exist and be a regular file
- the file policy size cap is enforced before reading

This endpoint is read-only. Attaching a file to session context remains the
responsibility of `POST /v1/sessions/{sid}/context/files`.
