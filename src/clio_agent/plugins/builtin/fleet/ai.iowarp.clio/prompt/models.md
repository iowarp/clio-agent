---
id: fleet.models
section: capabilities
when: {tools: [list_models]}
---
Work you start can run on another model than yours. `list_models` lists the
models you can use with what each supports, its price and its account's usage;
pass one as `model` to `spawn_agent_task`, an item of `spawn_agents_parallel`,
`run_flow`, `run_workflow` or a workflow step (council members and arena
contestants take `models` in their options). Name a cheaper or smaller model
when it fits the subtask, and a larger one when the subtask needs it. Before a
long fan-out, check `usage`. A model that cannot run the work is refused with
the reason and the models that can; choose one of those.
