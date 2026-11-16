# /optimize

`/optimize` is the capture procedure with a different input and output: from a plugin
and the sessions where it ran, to that plugin's next version. With no plugin named, it
works across all recent sessions.

## /optimize <plugin> [what to improve]

1. **Scope.** Call `optimize_scope(plugin="<id>")`: the root conversations of the last
   90 days where it ran (a tool or flow called, a skill loaded, a subagent started, its
   hooks active), each with that evidence. If the person named sessions with `@`, pass
   them as `sessions` instead. Show the person the scope (how many sessions, from when)
   before reading anything; a few sessions can be read here, more go to the
   `optimize-scan` flow as in the global run below.
2. **Read.** `session_record` with those session ids, then read the records in
   `run_code`. Look for:
   - the skill not loaded when the session needed it (a trigger miss);
   - a correction the person had to repeat;
   - a hook that refused what it should have allowed, or let through what it should
     have refused;
   - `run_code` written around one of the plugin's tools;
   - a step the person added by hand every time;
   - a fragment the agent ignored.
3. **Falling through to capture.** When the evidence shows a procedure the plugin does
   not cover at all, not a fix to what it does, say so and ask whether to grow this
   plugin or start a new one. Either way that piece goes through steps 3 to 5 of the
   capture procedure (constants and variables, the parts plan, the detail questions)
   before anything is written.
4. **Propose the changes** as in step 4 of capture: per change, the evidence (sessions
   linked, what happened) and the part it changes.
5. **Write.** A plugin Clio wrote: `make_plugin` with the same name makes its next
   version, with the diff recorded. A plugin from any other marketplace (including
   CLIO's own, such as `core`): the person's variation under a new name, with
   `variation_of="<marketplace>/<plugin>"`. The original is never edited. When the
   original brings tools or servers, a copy would collide with it: the variation then
   requires the original and adds only the change, and still names it in
   `variation_of`.
6. **Review** as in step 7 of capture.
7. **Trial: old against new.** The tasks come from the sessions where the current
   version fell short. Run each with the current version and with the new one, side by
   side. While the new version waits for trust, the child carrying it is asked about
   once and only its changed executable parts are named as changed.
8. **Hand over** as in step 9 of capture.

## /optimize (no plugin): the global run

1. **Scope.** `optimize_scope()`: every root conversation since the last global
   `/optimize` (the account's watermark; the first run reads the last 30 days). The
   person can name sessions with `@` (`sessions`) or give a window (`since` or `days`).
   Show the scope first: how many sessions, from when, and the findings declined before.
2. **Scan.** Run the `optimize-scan` flow:
   `run_flow(flow="optimize-scan", input={"sessions": <the scope's sessions>, "declined": <declined>})`.
   It gives each session to an `optimize-scanner` child in parallel, on the person's
   `optimize` fleet role. Each child writes its session's record, reads it in code and
   returns findings with evidence. A session whose scan did not finish is listed with
   its reason; do not drop it. With few sessions or without `run_flow`, scan them here
   yourself in `run_code`.
3. **Attribute.** Each finding goes to one place:
   - a plugin part that was involved and went wrong (a skill followed into a mistake, a
     tool that failed or was worked around, a hook that refused wrongly or let through
     what it should have refused);
   - a plugin part that should have been involved: the plugin was in that session's
     agent and one of its skills or tools fit the task, but was never used (a trigger
     miss);
   - a gap: no carried or installed plugin covers it. Group gaps from different
     sessions that describe the same procedure into one;
   - not fixable by a plugin: a CLIO defect, a sandbox refusal, a provider error.
     These are listed and not acted on.
4. **Rank.** Recurrence decides what becomes a proposal. A finding seen in one session
   is listed but not proposed, unless it is severe (data written where it should not
   have been, a destructive command that ran).
5. **Report.** One report: per plugin, its findings with counts and links to the
   sessions; then the gaps, each with the parts it would become; then what no plugin
   can fix. The person picks what to act on with a `ChoicePicker` (several may be
   picked). Then call `optimize_report_shown(declined=[...])` with the findings the
   person did not pick (`key`, `plugin`, `summary`, `sessions`): the watermark moves to
   the end of the scope, so a later run does not report the same sessions again, and a
   declined finding is not proposed again unless it recurs in new sessions.
6. **Act.** Each picked plugin goes through `/optimize <plugin>` above with the findings
   already gathered (skip its own scope and reading). Each picked gap goes through
   capture from step 2, with the grouped sessions as its record. Drafts can be prepared
   by children in parallel, but questions reach the person one plugin or gap at a time.
   A finding against `core` or any other built-in becomes the person's variation.
