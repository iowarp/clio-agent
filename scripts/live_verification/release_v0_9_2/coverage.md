# Live coverage matrix

Each row requires a runtime record plus Browser screenshots named
`NN-initial.png`, `NN-intermediate.png`, `NN-final.png`, and `NN-reloaded.png`
inside the scenario evidence directory.

| Gate | Blueprint | Required visible behavior | Runtime evidence |
|---|---|---|---|
| Candidate identity | Base Agent | Correct versions and pinned components | SHA and version manifest |
| Progressive reload | Base Agent | Shell, cached content, streamed hydration, usable controls | reload milestones and session events |
| Auto/manual compaction | Base Agent | Complete transcript and ordered checkpoints after restart | ARC, message, checkpoint, and lane assertions |
| EarthScope | EarthScope Skills | Ranked map, human selection, interactive time series, PNG and report | exact NDP/geo paths, A2UI ready states, artifacts |
| Factorio Flat | Factorio Flat | Six Daisy specialists, child graph, hierarchical Gantt, report and figure | child identities, Observe/Wait/Collect, grounded analysis |
| Files and artifacts | Base Agent | Boxed rows, side-panel navigation, independent category scrolling | file/change/artifact records |
| Resources | Base Agent | Source identity, conversion target, labeled search input, bounded result | resource list/inspect/search/read/wait results |
| Memory | Base Agent | SPA navigation, collapsible matches, rendered summary | memory search and session-summary records |
| Governance | Base Agent | Plan, todo, goal, loop and schedule state | typed state transitions and final outcome |
| Skills/workflows | Tool UI Qualification Workflow | Clickable SKILL.md, workflow identity/definition, async steps, graph/Gantt | skill and workflow records |
| Delegation | Tool UI Cross-file Triage | Child navigation and boxed Spawn/Observe/Message/Wait/Collect | child event stream and collected output |
| Providers | Base Agent | Compact expandable provider/model groups | provider refresh result |
| MCP Apps | V2EX Avenues | App open, payload, ui/message action, reload | persisted mcp_app part and resource route |

Missing semantics, wrong data, broken navigation, unbounded output, inaccessible
evidence, or page-wide blocking loaders are functional failures. Cosmetic polish
that preserves meaning and access is recorded as a non-blocking follow-up.
