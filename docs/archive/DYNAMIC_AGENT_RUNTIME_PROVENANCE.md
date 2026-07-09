# Dynamic Agent Runtime Provenance

CLIO records dynamic user/skill agent execution details on assistant message
metadata under `agent_runtime`.

This is intentionally non-secret runtime provenance. It tells clients and later
context-frame code which agent actually ran, which prompt source it used, which
tool allowlist applied, and whether its model/provider came from the agent
definition or fell back to the active global model.

## Shape

```json
{
  "agent_runtime": {
    "kind": "dynamic_agent",
    "agent_id": "reviewer",
    "source": "user",
    "title": "Reviewer",
    "execution_mode": "prompt_agent",
    "tools": [],
    "prompt": {
      "source": "agent_definition",
      "has_system_prompt": true
    },
    "model": {
      "provider_id": "openai",
      "model_id": "gpt-4.1",
      "provider_source": "agent_default",
      "model_source": "agent_default",
      "fallback_to_global": false
    }
  }
}
```

`execution_mode` is `prompt_agent` for prompt-only dynamic agents and
`tool_agent` for agents with declared tools.

`fallback_to_global` is true when either the provider or model was inherited
from CLIO's active global LM instead of being fully specified on the agent.

## Consumers

The field is designed for:

- TUI routing/expert badges.
- context-frame and memory-truth records.
- prompt/profile debugging.
- model fallback warnings.
- command and user-defined skill provenance.

The field does not include prompt text, API keys, or provider credentials.
