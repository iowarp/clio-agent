# Chemistry in CLIO: deferred work

Parts of the "Chemistry in CLIO: conclusions (Nov 15, 2026)" plan that were removed
from the first implementation session (session_01BHQAwt26SXevc5vL25FMh8, 2026-11-16).
Model safeguards stopped that session's work on them, and the user chose to drop them
there and come back to them later, on another model (Sonnet), once everything else is
done. Nothing here was started; no code for them exists on any branch.

| Item | Where it belonged | Plan reference |
|---|---|---|
| The `vina` MCP server | clio-kit `mcp-servers/vina/` | "MCP servers in clio-kit" table |
| The `vina` program plugin and its skill | marketplace `plugins/vina/` | "Program and source plugins" table |
| The `docking-study` skill | marketplace `plugins/alembic/` | "`alembic`" skill list |
| The AutoDock Vina connector name in `derive.yaml` | clio-agent `catalogs/connectors/clio-kit/derive.yaml` | "How each server reaches a conversation" |
| Burette's docking view and pose-comparison template | Burette fork `packages/clio-burette/` | "Burette, forked" (last sentence) |
| Test case C9 | ledger | "Test cases" |
| The second SC demo video | — | "Goal for SC", second candidate demo |

Also deferred from the same session for the same reason, but expected to be fine to do
in a fresh session: the `fdmnes` and `graspa` servers and the gRASPA and FDMNES Spack
recipes. These are listed under "Remaining" in
[chemistry-in-clio-status.md](chemistry-in-clio-status.md), not here.

When picking these up: read the plan's sections named above, keep the same contracts as
the shipped servers (see the status file), add the Alembic agent's plugin list entry, and
qualify the case with a plain request.
