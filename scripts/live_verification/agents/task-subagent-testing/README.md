# Task and Subagent Qualification

Live-verification-only qualification coordinator (`scripts/live_verification/`).
Follow only the supplied mission; never reinterpret a failed gate as success.
The agent carries `core`, `workspace`, `shell` and `delegation`; its
`task-subagent-testing` plugin defines the `worker` subagent
(`spawn_agent_task(agent="worker", ...)`) with `shell_bash` and `fs_read_file`.

Layout: a native marketplace (`ai.iowarp.clio/marketplace.json`, `plugins/task-subagent-testing/`, `agents/task_subagent_testing.py`), installed by `_common.py::install_testing_agent` as marketplace `lv-task-subagent-testing` and selected as agent `lv-task-subagent-testing/task-subagent-testing`. See `docs/PLUGINS_AND_FLOWS.md`.
