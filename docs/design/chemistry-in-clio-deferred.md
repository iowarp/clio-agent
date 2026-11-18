# Chemistry in CLIO: deferred work

Parts of the "Chemistry in CLIO: conclusions (Nov 15, 2026)" plan that were removed
from the first implementation session (session_01BHQAwt26SXevc5vL25FMh8, 2026-11-16).
Model safeguards stopped that session's work on them, and the user chose to drop them
there and come back to them later, on another model (Sonnet), once everything else is
done. Nothing here was started; as of the merge of clio-agent #2370 (2026-11-17), no code for
them exists on any branch.

| Item | Where it belonged | Plan reference |
|---|---|---|
| The `vina` MCP server | clio-kit `mcp-servers/vina/` | "MCP servers in clio-kit" table |
| The `vina` program plugin and its skill | marketplace `plugins/vina/` | "Program and source plugins" table |
| The `docking-study` skill | marketplace `plugins/alembic/` | "`alembic`" skill list |
| The AutoDock Vina connector name in `derive.yaml` | clio-agent `catalogs/connectors/clio-kit/derive.yaml` | "How each server reaches a conversation" |
| Burette's docking view and pose-comparison template | Burette fork `packages/clio-burette/` | "Burette, forked" (last sentence) |
| Test case C9 | ledger | "Test cases" |
| The second SC demo video | — | "Goal for SC", second candidate demo |

The `fdmnes` and `graspa` servers and their Spack recipes, deferred in that session for the
same reason, have since shipped (clio-kit #432) and are not part of this list.

When picking these up: read the plan's sections named above, keep the same contracts as
the shipped servers (see the status file), add the Alembic agent's plugin list entry, and
qualify the case with a plain request. The closest model to follow is the shipped `fdmnes`
server (clio-kit `mcp-servers/fdmnes`) and plugin (marketplace `plugins/fdmnes`); add
`docking-study` to the Alembic mission's list of study skills too.
