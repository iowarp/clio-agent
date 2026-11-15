# Footprints, part models and enclosures: ledger

This ledger holds the decisions and acceptance evidence for
[the footprints and enclosure plan](kicad-footprints-plan.md). Decisions use `FP`
(footprints and models), `EN` (envelope and enclosure) and `FV` (the view); qualification
rows use `FQ`. The person's messages are quoted in the appendix.

## Header

The base is where [the KiCad plan](kicad-plan.md) finishes. The heads are filled in when this
work starts.

| Repository | Branch | Base |
| --- | --- | --- |
| kicad-mcp-pro (fork) | `claude/magical-wozniak-ub0nxg` | the KiCad plan's last commit |
| clio-agent | `claude/magical-wozniak-ub0nxg` | the KiCad plan's last commit |
| gact-tui | `claude/magical-wozniak-ub0nxg` | the KiCad plan's last commit |
| clio-agent-marketplace | `claude/magical-wozniak-ub0nxg` | the KiCad plan's last commit |
| KiCad Library Tools | `main`, pinned | `090252c1f2429ceb8bd8ef2aa466b6c135f6273a` (`main` on 15 Nov 2026) |

## Reachability (15 Nov 2026)

- gitlab.com answers this session: `git ls-remote` on Library Tools returns `main`, so the
  plan's owner-work row is not needed here.
- PyPI has CadQuery 2.8.0 and KicadModTree 1.1.2.

## Decisions from the person's messages

| ID | Decision | Where | Status |
| --- | --- | --- | --- |
| FA-D1 | **The plan's mechanisms are a starting point; what works on a real board wins.** If a fixed measuring script cannot measure a board reliably, the agent writes the measurement itself from the board's STEP, and the skill teaches how to measure rather than which script to run. Where a render of the board or the case improves a measurement or a fit check, the skill says when to take one and what to look for. Each such change is recorded here and the plan is updated in the same change (message 1) | plan EN1, EN3, EN4; skills `pcb-enclosure`, `pcb-footprints` | open |

## Decisions

| ID | Decision | Where | Evidence | Status |
| --- | --- | --- | --- | --- |

## Qualification

| ID | Result | Evidence |
| --- | --- | --- |

## Appendix: the person's messages

### Message 1 (15 Nov 2026)

> tho implement the plan based on wha tyou see, if the script idea fails and the agent is the
> one that needs to build the script to measure the distances that is okay, if an image taken
> from teh rendering works to improve that, then bring that up on the skill, etc
