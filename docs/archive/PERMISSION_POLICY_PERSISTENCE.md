# Permission Policy Persistence

`/v1/policies` stores declarative permission rules that the GACT tool gate
enforces before falling back to per-tool permission defaults. These rules are
safety configuration, so CLIO persists them beside the GACT session store in
`permission_policies.json`.

## Stored Shape

```json
{
  "policies": [
    {
      "scope": "workspace",
      "scope_id": "ws_default",
      "tool_name_pattern": "shell.*",
      "path_pattern": "/tmp/*",
      "action": "ask"
    }
  ]
}
```

## Write Semantics

- `PUT /v1/policies` validates the entire replacement list.
- If validation succeeds, CLIO replaces the in-memory list and flushes the JSON
  file atomically.
- If validation fails, CLIO leaves both the in-memory list and persisted file
  unchanged.
- On startup, malformed persisted rows are ignored rather than disabling the
  backend.

This keeps user-configured allow, deny, and ask policy semantics stable across
GACT backend restarts.
