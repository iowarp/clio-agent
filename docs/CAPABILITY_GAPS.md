# CLIO Capability Gaps

Tracking issue: https://github.com/iowarp/clio-agent/issues/327

## Purpose

CLIO should keep future ideas visible without making them look runnable. The
GACT capability flags already report `voice=false` and `lsp=false`; this
document and the companion endpoint explain what those false values mean.

## Endpoint

`GET /v1/capability-gaps`

Response:

```json
{
  "capability_gaps": {
    "voice": {
      "status": "unsupported",
      "advertised": false,
      "client_behavior": "render_disabled"
    }
  }
}
```

The same rows are also embedded in:

`GET /v1/capabilities -> capabilities.x_clio_capability_gaps`

## Current Rows

| Capability | Status | Expected Client Behavior |
| --- | --- | --- |
| `voice` | `unsupported` | Keep voice affordances hidden or disabled; text input remains the supported path. |
| `lsp` | `unsupported` | Keep language-server affordances hidden or disabled; use CLIO file/diff/tool surfaces instead. |

## Contract

- `status=unsupported` means the feature is not wired today.
- `advertised=false` mirrors the corresponding boolean capability flag.
- `client_behavior=render_disabled` means a TUI may keep the idea visible but
  must not render it as runnable.
- `related_endpoints` names the planned or absent surface so help/doctor views
  can explain exactly what is missing.
