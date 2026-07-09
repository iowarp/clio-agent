# Context File Turn Provenance

When a session has attached context files, CLIO records the files considered for
the turn on the assistant message under `metadata.context_files`.

The metadata is meant for TUI context inspectors, memory/context-frame records,
and file-mention debugging. It does not duplicate file contents.

## Shape

```json
{
  "context_files": {
    "status": "prepared",
    "count": 1,
    "max_inline_bytes": 32768,
    "files": [
      {
        "path": "/workspace/notes.md",
        "mode": "read",
        "status": "prepared",
        "inline_policy": "inline_or_inspect",
        "added_at": "2026-05-27T19:00:00Z"
      }
    ]
  }
}
```

`status` is `prepared` when context preparation completed and `error` when the
turn failed while resolving, inspecting, or reading an attached context file.

`inline_policy` is:

- `inline_or_inspect` for `read` and `pin` files. Text files are inlined up to
  `max_inline_bytes`; known scientific binary files are represented by
  structured inspection summaries.
- `metadata_only` for `edit` targets. The path is visible to the model but file
  contents are not inlined automatically.

If context preparation fails, the assistant turn still includes
`metadata.context_files` with `status: "error"` alongside the structured
`context_file_error`.
